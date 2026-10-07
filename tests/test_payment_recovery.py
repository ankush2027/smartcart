"""Offline inbox/recovery durability and races on independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from threading import Barrier, Event

import pytest

from edge.api.server import create_app
from edge.billing import checkout
from edge.database import database
from edge.payment import store, webhook_store, work
from edge.payment.diagnostics import report
from edge.payment.fake import FakeProvider
from edge.payment.provider import InvalidEvidence
from edge.payment.recovery import RecoveryWorker
from edge.payment.service import PaymentService
from edge.payment.webhook import WebhookIngestor
from edge.payment.webhook_processor import WebhookProcessor
from tests.test_checkout import isolated, populated, sql, state
from tests.test_payment import payment, reserve, start
from tests.test_razorpay import no_network


class Clock:
    value = 1000000

    def __call__(self):
        return self.value


class Crash(BaseException):
    pass


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ready(payment):
    attempt = start(payment)
    paid = payment[2].record_payment(attempt['provider_order_id'])
    return payment, attempt, paid


def delivery(provider, paid, *, event_id='event1', event_type='payment.captured', **changes):
    event = dict(event_id=event_id, event_type=event_type, order_id=paid.order_id, payment_id=paid.payment_id)
    event.update(changes)
    body = json.dumps(event).encode()
    return body, provider._signature(body)


def ingest(ready, clock, **changes):
    payment, attempt, paid = ready
    body, signature = delivery(payment[2], paid, **changes)
    return WebhookIngestor(payment[2], clock=clock).ingest(body, signature, event_id=changes.get('event_id', 'event1'))


def processor(payment, clock, **kwargs):
    return WebhookProcessor({payment[2].scope: payment[3]}, clock=clock, **kwargs)


def recovery(payment, clock, **kwargs):
    return RecoveryWorker({payment[2].scope: payment[3]}, clock=clock, **kwargs)


def test_durable_authenticated_insert_duplicate_restart(ready, clock):
    payment, attempt, paid = ready
    first = ingest(ready, clock)
    row = webhook_store.read(first['webhook_id'])
    assert row['raw_payload'] == delivery(payment[2], paid)[0]
    assert row['state'] == 'RECEIVED' and row['processing_attempts'] == 0
    before = state()
    database.initialize_database()
    assert state() == before
    duplicate = ingest(ready, clock)
    assert duplicate == dict(webhook_id=first['webhook_id'], state='RECEIVED', duplicate=True)
    assert state() == before
    assert processor(payment, clock).run_once(first['webhook_id']) == 'PROCESSED'
    assert store.read_attempt(attempt['payment_attempt_id'])['state'] == 'COMPLETED'
    assert processor(payment, clock).run_once(first['webhook_id']) == 'SKIPPED'


@pytest.mark.parametrize('kind', ['signature', 'altered', 'missing_id', 'other_id', 'malformed', 'oversized', 'scope'])
def test_untrusted_delivery_never_persisted(ready, clock, monkeypatch, kind):
    payment, attempt, paid = ready
    body, signature = delivery(payment[2], paid)
    event_id = 'event1'
    if kind == 'signature': signature = '0' * 64
    if kind == 'altered': body += b' '
    if kind == 'missing_id': event_id = None
    if kind == 'other_id': event_id = 'other'
    if kind == 'malformed': body = b'[]'; signature = payment[2]._signature(body)
    if kind == 'oversized': body = b'x' * 1048577
    if kind == 'scope':
        original = payment[2].authenticate_webhook
        monkeypatch.setattr(payment[2], 'authenticate_webhook',
                            lambda *a, **kw: replace(original(*a, **kw), provider_scope='wrong:test:account'))
    before = state()
    with pytest.raises(InvalidEvidence):
        WebhookIngestor(payment[2], clock=clock).ingest(body, signature, event_id=event_id)
    assert state() == before


def test_conflicting_digest_keeps_both_payloads_and_quarantines(ready, clock):
    first = ingest(ready, clock)
    original = webhook_store.read(first['webhook_id'])['raw_payload']
    result = ingest(ready, clock, event_type='payment.failed')
    assert result['state'] == 'QUARANTINED'
    assert webhook_store.read(first['webhook_id'])['raw_payload'] == original
    assert len(sql('SELECT raw_payload FROM payment_webhook_conflicts')) == 1
    assert processor(ready[0], clock).run_once(first['webhook_id']) == 'SKIPPED'
    ingest(ready, clock, event_type='payment.failed')
    assert len(sql('SELECT raw_payload FROM payment_webhook_conflicts')) == 1
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


def test_atomic_ingestion_failure_no_acceptance(ready, clock):
    sql("CREATE TRIGGER fail_insert AFTER INSERT ON payment_webhooks BEGIN SELECT RAISE(ABORT,'injected'); END")
    before = state()
    with pytest.raises(sqlite3.Error):
        ingest(ready, clock)
    assert state() == before


def test_commit_failure_no_acknowledgement(ready, clock, monkeypatch):
    original = work.get_connection
    class Connection:
        def __init__(self): self.conn = original()
        def execute(self, *a): return self.conn.execute(*a)
        def commit(self): raise sqlite3.OperationalError('injected commit failure')
        def rollback(self): self.conn.rollback()
        def close(self): self.conn.close()
    monkeypatch.setattr(work, 'get_connection', Connection)
    with pytest.raises(sqlite3.OperationalError):
        ingest(ready, clock)
    assert sql('SELECT COUNT(*) FROM payment_webhooks') == [(0,)]


def test_lost_ack_after_commit_is_duplicate(ready, clock, monkeypatch):
    original = webhook_store.insert_authenticated
    def lost(*a, **kw):
        original(*a, **kw)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(webhook_store, 'insert_authenticated', lost)
        with pytest.raises(Crash): ingest(ready, clock)
    assert ingest(ready, clock)['duplicate'] is True
    assert sql('SELECT COUNT(*) FROM payment_webhooks') == [(1,)]


def test_concurrent_duplicate_delivery_has_one_record(ready, clock):
    barrier = Barrier(6)
    def send():
        barrier.wait(5)
        return ingest(ready, clock)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: send(), range(6)))
    assert len({r['webhook_id'] for r in results}) == 1
    assert sum(not r['duplicate'] for r in results) == 1


def test_unknown_order_retry_then_quarantine(ready, clock):
    first = ingest(ready, clock, order_id='unknown-order')
    worker = processor(ready[0], clock, policy=work.RetryPolicy(max_attempts=2))
    assert worker.run_once(first['webhook_id']) == 'RETRYABLE'
    assert webhook_store.read(first['webhook_id'])['error_code'] == 'unknown_order'
    assert worker.run_once(first['webhook_id']) == 'SKIPPED'
    clock.value += 30
    assert worker.run_once(first['webhook_id']) == 'QUARANTINED'
    assert webhook_store.read(first['webhook_id'])['error_code'] == 'retry_exhausted'
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


def test_missing_scope_does_not_select_another_service(ready, clock):
    identity = ingest(ready, clock)['webhook_id']
    assert WebhookProcessor({}, clock=clock).run_once(identity) == 'RETRYABLE'
    assert webhook_store.read(identity)['error_code'] == 'scope_unavailable'


@pytest.mark.parametrize('stage,expected', [('fetch','ORDER_READY'), ('save','ORDER_READY'),
    ('after_save','VERIFIED'), ('complete','VERIFIED'), ('after_complete','COMPLETED'), ('finish','COMPLETED')])
def test_worker_crash_explicit_reclaim_and_restart(ready, clock, monkeypatch, stage, expected):
    payment, attempt, paid = ready
    identity = ingest(ready, clock)['webhook_id']
    target, name = {'fetch': (payment[2], 'fetch_payment'), 'save': (store, 'save_verified'),
        'after_save': (store, 'save_verified'), 'complete': (payment[3], 'complete'),
        'after_complete': (payment[3], 'complete'), 'finish': (work, 'finish')}[stage]
    original = getattr(target, name)
    def crash(*a, **kw):
        if stage.startswith('after_'): original(*a, **kw)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(target, name, crash)
        with pytest.raises(Crash): processor(payment, clock).run_once(identity)
    assert store.read_attempt(attempt['payment_attempt_id'])['state'] == expected
    assert webhook_store.read(identity)['state'] == 'PROCESSING'
    assert processor(payment, clock).run_once(identity, reclaim_expired=True) == 'SKIPPED'
    clock.value += 121
    assert processor(payment, clock).run_once(identity) == 'SKIPPED'  # No automatic lease stealing.
    restarted = PaymentService(FakeProvider(payment[2].ledger))
    worker = WebhookProcessor({payment[2].scope: restarted}, clock=clock)
    assert worker.run_once(identity, reclaim_expired=True) == 'PROCESSED'
    assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
    assert webhook_store.read(identity)['processing_attempts'] == 2


def test_superseded_claim_cannot_finish_or_mutate(ready, clock):
    payment, attempt, paid = ready
    identity = ingest(ready, clock)['webhook_id']
    policy = work.RetryPolicy()
    old = work.claim('payment_webhooks', identity, policy=policy, clock=clock)
    clock.value += 121
    new = work.claim('payment_webhooks', identity, policy=policy, clock=clock, reclaim_expired=True)
    assert old['claim_token'] != new['claim_token']
    fence = work.guard('payment_webhooks', old, clock=clock)
    with pytest.raises(work.ClaimLost):
        store.save_verified(attempt['payment_attempt_id'], paid, guard=fence)
    with pytest.raises(work.ClaimLost):
        work.finish('payment_webhooks', old, 'PROCESSED', policy=policy, clock=clock)
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


def test_conflict_during_provider_fetch_fences_payment(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    identity = ingest(ready, clock)['webhook_id']
    entered, release = Event(), Event()
    original = payment[2].fetch_payment
    def held(pid):
        entered.set()
        assert release.wait(5)
        return original(pid)
    monkeypatch.setattr(payment[2], 'fetch_payment', held)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(processor(payment, clock).run_once, identity)
        assert entered.wait(5)
        try:
            assert ingest(ready, clock, event_type='payment.failed')['state'] == 'QUARANTINED'
        finally:
            release.set()
        assert running.result(5) == 'CLAIM_LOST'
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


@pytest.mark.parametrize('field,value', [('amount_paise', 2001), ('amount_paise', True),
    ('currency', 'USD'), ('checkout_id', 999), ('payment_attempt_id', 'wrong'),
    ('provider_scope', 'wrong:test:scope'), ('order_id', 'wrong'), ('payment_id', 'wrong')])
def test_webhook_fetch_must_match_exact_persisted_attempt(ready, clock, field, value):
    payment, attempt, paid = ready
    payment[2].ledger.payments[paid.payment_id] = replace(paid, **{field: value})
    identity = ingest(ready, clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'QUARANTINED'
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


@pytest.mark.parametrize('status', ['AUTHORIZED', 'FAILED', 'REFUNDED'])
def test_webhook_status_never_authorizes_payment(ready, clock, status):
    payment, attempt, paid = ready
    payment[2].ledger.payments[paid.payment_id] = replace(paid, status=status)
    identity = ingest(ready, clock)['webhook_id']  # JSON still says captured.
    assert processor(payment, clock).run_once(identity) == 'RETRYABLE'
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


def test_out_of_order_and_delayed_events_are_idempotent(ready, clock):
    payment, attempt, paid = ready
    first = ingest(ready, clock, event_type='payment.failed')['webhook_id']
    assert processor(payment, clock).run_once(first) == 'PROCESSED'  # Fetch says captured.
    second = ingest(ready, clock, event_id='event2', event_type='payment.authorized')['webhook_id']
    before = sql('SELECT * FROM payment_attempts')
    payment[2].ledger.payments.clear()  # Completed replay does not require a fresh provider fetch.
    assert processor(payment, clock).run_once(second) == 'PROCESSED'
    assert sql('SELECT * FROM payment_attempts') == before


def test_cancelled_equal_amount_generation_cannot_be_rebound(payment, clock):
    sid, cid, provider, service = payment
    checkout.cancel_checkout(sid, cid, 'owner')
    current = checkout.begin_checkout(sid, 'owner', 'new-generation').checkout_id
    attempt = service.start(sid, current, 'owner', 'payment')
    paid = provider.record_payment(attempt['provider_order_id'])
    provider.ledger.payments[paid.payment_id] = replace(paid, checkout_id=cid)
    identity = ingest((payment, attempt, paid), clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'QUARANTINED'
    assert sql('SELECT state FROM checkouts ORDER BY checkout_id') == [('CANCELLED',), ('FROZEN',)]


def test_provider_io_never_holds_sqlite_writer(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    original = payment[2].fetch_payment
    def fetch(pid):
        with closing(database.get_connection()) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN EXCLUSIVE')
            conn.rollback()
        return original(pid)
    monkeypatch.setattr(payment[2], 'fetch_payment', fetch)
    identity = ingest(ready, clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'PROCESSED'


def test_reserved_worker_dispatch_once_then_waits_for_evidence(payment, clock):
    attempt = reserve(payment)
    worker = recovery(payment, clock)
    assert worker.run_once(attempt['payment_attempt_id']) == 'RETRYABLE'
    assert payment[2].create_calls == 1
    assert worker.run_once(attempt['payment_attempt_id']) == 'SKIPPED'
    clock.value += 30
    assert worker.run_once(attempt['payment_attempt_id']) == 'RETRYABLE'
    assert payment[2].create_calls == 1
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]


def test_creating_worker_never_redispatches(payment, clock):
    payment[2].creation = 'timeout'
    attempt = start(payment)
    for _ in range(3):
        assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'RETRYABLE'
        clock.value += 30
    assert payment[2].create_calls == 1
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(1,)]


def test_webhook_before_order_response_is_recovered(payment, clock):
    payment[2].creation = 'lost_response'
    attempt = start(payment)
    remote = next(iter(payment[2].ledger.orders.values()))
    paid = payment[2].record_payment(remote.order_id)
    identity = ingest((payment, attempt, paid), clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'RETRYABLE'
    clock.value += 30
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'PROCESSED'
    assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
    assert payment[2].create_calls == 1


def test_verified_recovery_needs_no_provider_fetch(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    store.save_verified(attempt['payment_attempt_id'], paid)
    monkeypatch.setattr(payment[2], 'fetch_payment', lambda *a: pytest.fail('Unexpected provider fetch'))
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'PROCESSED'
    before = state()
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'COMPLETED'
    assert state() == before


def test_failed_attempt_not_resurrected(payment, clock):
    payment[2].creation = 'reject'
    attempt = start(payment)
    before = state()
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'FAILED'
    assert state() == before and payment[2].create_calls == 1


def test_callback_webhook_recovery_race_single_completion(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    sid, cid, provider, service = payment
    evidence = provider.evidence(paid.payment_id)
    store.save_verified(attempt['payment_attempt_id'], paid)
    identity = ingest(ready, clock)['webhook_id']
    completions = []
    service.completion_sync = lambda sid: completions.append(sid)
    barrier = Barrier(3)
    original = store.complete_paid_checkout
    def racing(*a, **kw):
        barrier.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(store, 'complete_paid_checkout', racing)
    with ThreadPoolExecutor(3) as pool:
        callback = pool.submit(service.submit_evidence, sid, cid, 'owner', evidence)
        webhook = pool.submit(processor(payment, clock).run_once, identity)
        worker = pool.submit(recovery(payment, clock).run_once, attempt['payment_attempt_id'])
        assert callback.result(10)['state'] == 'COMPLETED'
        assert webhook.result(10) == 'PROCESSED'
        assert worker.result(10) == 'PROCESSED'
    assert completions == [sid]
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(1,)]


def test_callback_webhook_verification_race(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    sid, cid, provider, service = payment
    evidence = provider.evidence(paid.payment_id)
    identity = ingest(ready, clock)['webhook_id']
    barrier = Barrier(2)
    original = provider.fetch_payment
    def fetch(pid):
        barrier.wait(5)
        return original(pid)
    monkeypatch.setattr(provider, 'fetch_payment', fetch)
    with ThreadPoolExecutor(2) as pool:
        callback = pool.submit(service.submit_evidence, sid, cid, 'owner', evidence)
        webhook = pool.submit(processor(payment, clock).run_once, identity)
        assert callback.result(10)['state'] == 'COMPLETED'
        assert webhook.result(10) == 'PROCESSED'


def test_firebase_hook_failure_after_commit_does_not_repay(ready, clock):
    payment, attempt, paid = ready
    calls = []
    def failed_sync(sid):
        assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
        calls.append(sid)
        raise RuntimeError('dummy-secret-do-not-log')
    payment[3].completion_sync = failed_sync
    identity = ingest(ready, clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'PROCESSED'
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id']) == 'COMPLETED'
    assert len(calls) == 1


def test_readonly_diagnostics_allowlist(ready, clock):
    payment, attempt, paid = ready
    recovery(payment, clock).run_once(attempt['payment_attempt_id'])
    before = state()
    result = report()
    assert state() == before
    assert set(result['payments'][0]) == {'payment_attempt_id','checkout_id','provider_scope',
                                        'payment_state','recovery_status','error_code'}
    assert result['payments'][0]['error_code'] == 'awaiting_evidence'
    assert 'owner' not in json.dumps(result) and 'raw_payload' not in json.dumps(result)


def test_diagnostics_never_creates_missing_database(tmp_path):
    target = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError): report(target)
    assert not target.exists()


def test_no_webhook_route_or_default_activation():
    app = create_app()
    assert not any('webhook' in str(rule) for rule in app.url_map.iter_rules())


@pytest.mark.parametrize('operation', ["DELETE FROM payment_webhooks",
    "UPDATE payment_webhooks SET raw_payload=x'01'", "UPDATE payment_webhooks SET provider_payment_id='other'"])
def test_inbox_evidence_immutable(ready, clock, operation):
    ingest(ready, clock)
    before = state()
    with pytest.raises(sqlite3.IntegrityError): sql(operation)
    assert state() == before


def test_replace_cannot_erase_inbox_history(ready, clock):
    identity = ingest(ready, clock)['webhook_id']
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql('INSERT OR REPLACE INTO payment_webhooks SELECT * FROM payment_webhooks WHERE webhook_id=?', (identity,))
    assert state() == before


def test_conflict_after_completion_preserves_paid_authority(ready, clock):
    identity = ingest(ready, clock)['webhook_id']
    assert processor(ready[0], clock).run_once(identity) == 'PROCESSED'
    before = sql('SELECT * FROM payment_attempts')
    assert ingest(ready, clock, event_type='payment.failed')['state'] == 'QUARANTINED'
    assert sql('SELECT * FROM payment_attempts') == before


def test_two_processors_claim_one_event(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    identity = ingest(ready, clock)['webhook_id']
    entered, release = Event(), Event()
    original = payment[2].fetch_payment
    def held(pid):
        entered.set()
        assert release.wait(5)
        return original(pid)
    monkeypatch.setattr(payment[2], 'fetch_payment', held)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(processor(payment, clock).run_once, identity)
        assert entered.wait(5)
        try:
            assert pool.submit(processor(payment, clock).run_once, identity).result(5) == 'SKIPPED'
        finally:
            release.set()
        assert first.result(5) == 'PROCESSED'
    assert webhook_store.read(identity)['processing_attempts'] == 1


def test_conflict_before_completion_stops_stale_worker(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    identity = ingest(ready, clock)['webhook_id']
    original = payment[3].complete
    def conflict(*args, **kwargs):
        ingest(ready, clock, event_type='payment.failed')
        return original(*args, **kwargs)
    monkeypatch.setattr(payment[3], 'complete', conflict)
    assert processor(payment, clock).run_once(identity) == 'CLAIM_LOST'
    assert sql('SELECT state FROM payment_attempts') == [('VERIFIED',)]
    assert sql('SELECT end_time FROM sessions')[0][0] is None


def test_recovery_crash_after_dispatch_claim_never_reposts(payment, clock, monkeypatch):
    attempt = reserve(payment)
    original = payment[2].create_order
    def crash(request):
        original(request)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(payment[2], 'create_order', crash)
        with pytest.raises(Crash): recovery(payment, clock).run_once(attempt['payment_attempt_id'])
    assert sql('SELECT state FROM payment_attempts') == [('CREATING',)]
    clock.value += 121
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id'], reclaim_expired=True) == 'RETRYABLE'
    assert sql('SELECT state FROM payment_attempts') == [('ORDER_READY',)]
    assert payment[2].create_calls == 1


def test_unexpected_provider_error_redacted_and_bounded(ready, clock, monkeypatch, caplog):
    payment, attempt, paid = ready
    def failed(*args): raise RuntimeError('dummy-sensitive-provider-error')
    monkeypatch.setattr(payment[2], 'fetch_payment', failed)
    identity = ingest(ready, clock)['webhook_id']
    assert processor(payment, clock).run_once(identity) == 'RETRYABLE'
    assert webhook_store.read(identity)['error_code'] == 'provider_unavailable'
    assert 'dummy-sensitive' not in json.dumps(report()) and 'dummy-sensitive' not in caplog.text


def test_migration_rollback_is_atomic(monkeypatch):
    from edge.payment import recovery as recovery_module
    for table in ('payment_webhook_conflicts', 'payment_webhooks', 'payment_recovery_jobs'):
        sql('DROP TABLE ' + table)
    before = state()
    original = recovery_module.migrate_recovery
    def fail(conn):
        original(conn)
        raise RuntimeError('injected after DDL')
    with monkeypatch.context() as patch:
        patch.setattr(recovery_module, 'migrate_recovery', fail)
        with pytest.raises(RuntimeError): database.initialize_database()
    assert state() == before
    database.initialize_database()
    assert sql('PRAGMA foreign_key_check') == []


@pytest.mark.parametrize('value', [0, -1, True, 101])
def test_bounded_batch_policy(value):
    with pytest.raises(ValueError): webhook_store.pending(limit=value)


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_invalid_retry_policy(value):
    with pytest.raises(ValueError): work.RetryPolicy(max_attempts=value)


@pytest.mark.parametrize('changes,expected', [({}, 'PROCESSED'),
    ({'amount_refunded': 1, 'refund_status': 'partial'}, 'RETRYABLE'),
    ({'status': 'authorized', 'captured': False}, 'RETRYABLE'),
    ({'amount': 1999}, 'RETRYABLE'), ({'currency': 'USD'}, 'RETRYABLE'),
    ({'order_id': 'order_other'}, 'RETRYABLE')])
def test_razorpay_signed_inbox_independently_fetches_payment(populated, clock, changes, expected):
    from tests.test_razorpay import adapter, MockHttp, response, payment as provider_payment, signature, WEBHOOK
    sid, _ = populated
    cid = checkout.begin_checkout(sid, 'owner', 'checkout').checkout_id
    remote = {}
    def create(method, url, args):
        remote.update(entity='order', id='order_dummy', **args['json'])
        return response(remote)
    http = MockHttp(create, response(provider_payment(**changes)), lambda *a: response(remote))
    provider = adapter(http, order_lookup=store.order_request)
    service = PaymentService(provider)
    attempt = service.start(sid, cid, 'owner', 'payment')
    # Even a signed payload claiming capture must be checked independently.
    body = json.dumps({'entity': 'event', 'account_id': 'acc_dummy', 'event': 'payment.captured',
                       'payload': {'payment': {'entity': provider_payment()}}}).encode()
    accepted = WebhookIngestor(provider, clock=clock).ingest(body, signature(body, WEBHOOK), event_id='evt_1')
    assert len(http.calls) == 1  # Authentication/ingestion did no HTTP.
    result = WebhookProcessor({provider.scope: service}, clock=clock).run_once(accepted['webhook_id'])
    assert result == expected
    assert store.read_attempt(attempt['payment_attempt_id'])['state'] == ('COMPLETED' if expected == 'PROCESSED' else 'ORDER_READY')


def test_razorpay_header_id_replay_cannot_duplicate_payment(populated, clock):
    from tests.test_razorpay import adapter, MockHttp, response, payment as provider_payment, signature, WEBHOOK
    sid, _ = populated
    cid = checkout.begin_checkout(sid, 'owner', 'checkout').checkout_id
    remote = {}
    def create(method, url, args):
        remote.update(entity='order', id='order_dummy', **args['json'])
        return response(remote)
    http = MockHttp(create, response(provider_payment()), lambda *a: response(remote))
    provider = adapter(http, order_lookup=store.order_request)
    service = PaymentService(provider)
    service.start(sid, cid, 'owner', 'payment')
    completions = []
    service.completion_sync = completions.append
    body = json.dumps({'entity': 'event', 'account_id': 'acc_dummy', 'event': 'payment.captured',
                       'payload': {'payment': {'entity': provider_payment()}}}).encode()
    for event_id in ('evt_original', 'evt_unsigned_header_changed'):
        accepted = WebhookIngestor(provider, clock=clock).ingest(body, signature(body, WEBHOOK), event_id=event_id)
        assert WebhookProcessor({provider.scope: service}, clock=clock).run_once(accepted['webhook_id']) == 'PROCESSED'
    assert completions == [sid] and len(http.calls) == 3


def test_quarantined_record_never_retries(ready, clock):
    identity = ingest(ready, clock, order_id='unknown')['webhook_id']
    worker = processor(ready[0], clock, policy=work.RetryPolicy(max_attempts=1))
    assert worker.run_once(identity) == 'QUARANTINED'
    before = state()
    clock.value += 10000
    assert worker.run_once(identity, reclaim_expired=True) == 'SKIPPED'
    assert state() == before


def test_recovery_reclaims_job_after_completion_commit(ready, clock, monkeypatch):
    payment, attempt, paid = ready
    store.save_verified(attempt['payment_attempt_id'], paid)
    original = payment[3].complete
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(payment[3], 'complete', crash)
        with pytest.raises(Crash): recovery(payment, clock).run_once(attempt['payment_attempt_id'])
    assert sql('SELECT state FROM payment_recovery_jobs') == [('PROCESSING',)]
    assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
    monkeypatch.setattr(payment[3], 'complete', lambda *a, **kw: pytest.fail('Must not complete again'))
    clock.value += 121
    assert recovery(payment, clock).run_once(attempt['payment_attempt_id'], reclaim_expired=True) == 'COMPLETED'
    assert sql('SELECT state FROM payment_recovery_jobs') == [('PROCESSED',)]
