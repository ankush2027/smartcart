"""Checkout history, exact-ID cancellation and cross-generation runtime safety."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event
import sqlite3

import pytest

from edge.billing import billing, checkout
from edge.database import database
from edge.pairing.session_store import pair_and_assign_in_transaction, IntegrityConflict
from tests.test_checkout import isolated, populated, sql, state, api
from tests.test_cart_runtime import runtime, acquire, dispatch_next


def freeze(sid, key='first'):
    return checkout.begin_checkout(sid, 'owner', key)


def cancel(sid, cid):
    return checkout.cancel_checkout(sid, cid, 'owner')


def post_cancel(client, sid, cid, body=None):
    return client.post(f'/sessions/{sid}/checkouts/{cid}/cancel', json={} if body is None else body,
                       headers={'Authorization': 'Bearer valid'})


def read(client, sid):
    return client.get(f'/sessions/{sid}/checkout', headers={'Authorization': 'Bearer valid'})


def test_core_30_to_40_history_and_old_requests_isolated(populated):
    sid, pid = populated
    billing.add_item(sid, pid)
    first = freeze(sid)
    assert first.final_amount == 30
    cancelled = cancel(sid, first.checkout_id)
    assert cancelled.state == 'CANCELLED' and cancelled.runtime_restart_required
    # Operator has reconciled physical contents; a newly hydrated runtime only.
    fresh = runtime()
    assert fresh.session._checkout_generation == first.checkout_id
    acquire(fresh, 75)
    assert dispatch_next(fresh).status == 'committed'
    second = freeze(sid, 'second')
    assert second.final_amount == 40 and second.checkout_id != first.checkout_id
    before = state()
    assert cancel(sid, first.checkout_id) == cancelled
    assert freeze(sid) == cancelled
    assert state() == before
    assert sql('SELECT amount,state FROM checkouts ORDER BY checkout_id') == [(30, 'CANCELLED'), (40, 'FROZEN')]
    assert sql('SELECT checkout_started_at FROM sessions') == [(second.checkout_started_at,)]


def test_remove_and_equal_amount_generations(populated):
    sid, _ = populated
    first = freeze(sid)
    cancel(sid, first.checkout_id)
    equal = freeze(sid, 'equal')
    assert equal.final_amount == first.final_amount and equal.checkout_id != first.checkout_id
    cancel(sid, equal.checkout_id)
    fresh = runtime()
    acquire(fresh, -75)
    assert dispatch_next(fresh).status == 'committed'
    third = freeze(sid, 'reduced')
    assert third.final_amount == 10
    assert sql('SELECT amount FROM checkouts ORDER BY checkout_id') == [(20,), (20,), (10,)]


@pytest.mark.parametrize('kind', ['ADD', 'REMOVE'])
@pytest.mark.parametrize('inflight', [False, True])
def test_old_runtime_stays_invalid_after_cancel(populated, monkeypatch, kind, inflight):
    sid, _ = populated
    old = runtime()
    assert old.session._checkout_generation == 0
    before_memory = old.session.cart_state.snapshot()
    acquire(old, 75 if kind == 'ADD' else -75)
    acquire(old, 75, offset=2)
    def cycle():
        row = freeze(sid)
        cancel(sid, row.checkout_id)
    if inflight:
        import edge.cart_session as module
        name = '_billing_add' if kind == 'ADD' else '_billing_remove'
        original = getattr(module, name)
        def after_cycle(*args, **kwargs):
            assert kwargs['expected_generation'] == 0
            cycle()
            return original(*args, **kwargs)
        monkeypatch.setattr(module, name, after_cycle)
    else:
        cycle()
    outcomes = []
    old.on_outcome = outcomes.append
    old._work()
    assert old.failed.is_set() and old.stopping.is_set() and old.jobs.empty()
    assert len(outcomes) == 1 and 'Stale runtime generation' in outcomes[0].detail
    assert old.session.cart_state.snapshot() == before_memory
    assert billing.calculate_total(sid) == 20
    assert len(billing.get_session_log(sid)) == 2
    assert old.session._checkout_generation == 0


def test_new_generation_is_invalidated_by_second_checkout(populated):
    sid, _ = populated
    first = freeze(sid)
    cancel(sid, first.checkout_id)
    fresh = runtime()
    second = freeze(sid, 'second')
    cancel(sid, second.checkout_id)
    acquire(fresh, 75)
    result = dispatch_next(fresh)
    assert result.status == 'rejected' and 'Stale runtime generation' in result.detail
    assert billing.calculate_total(sid) == 20


def test_cancel_does_not_resume_or_authorize_physical_basket(populated):
    sid, _ = populated
    old = runtime()
    first = freeze(sid)
    result = cancel(sid, first.checkout_id)
    assert result.runtime_restart_required
    assert old.session._checkout_generation == 0  # no automatic rehydration
    assert read(api(), sid).json['runtime_restart_required'] is True
    with pytest.raises(billing.StaleRuntimeError):
        billing.add_item(sid, populated[1], expected_generation=0)


@pytest.mark.parametrize('operation', [billing.add_runtime_item, billing.remove_runtime_item])
@pytest.mark.parametrize('generation', [None, True, -1, '0'])
def test_runtime_entrypoints_cannot_omit_generation(populated, operation, generation):
    before = state()
    with pytest.raises(billing.StaleRuntimeError):
        operation(*populated, expected_generation=generation)
    with pytest.raises(TypeError):
        operation(*populated)
    assert state() == before


@pytest.mark.parametrize('key', [None, '', 'has space', '\n', 'x' * 129, '₹', 'legacy:1'])
def test_invalid_idempotency_keys(populated, key):
    sid, _ = populated
    before = state()
    with pytest.raises(checkout.CheckoutError) as error:
        checkout.begin_checkout(sid, 'owner', key)
    assert error.value.code == 'invalid_idempotency_key'
    assert state() == before


def test_http_key_required_new_key_conflict_and_restart_history(populated):
    sid, _ = populated
    client = api()
    url = f'/sessions/{sid}/checkout'
    auth = {'Authorization': 'Bearer valid'}
    assert client.post(url, json={}, headers=auth).status_code == 400
    first = client.post(url, json={}, headers={**auth, 'Idempotency-Key': 'first'})
    assert first.status_code == 200
    assert client.post(url, json={}, headers={**auth, 'Idempotency-Key': 'other'}).status_code == 409
    cancelled = post_cancel(client, sid, first.json['checkout_id'])
    assert cancelled.status_code == 200
    database.initialize_database()
    assert post_cancel(api(), sid, first.json['checkout_id']).json == cancelled.json
    assert api().post(url, json={}, headers={**auth, 'Idempotency-Key': 'first'}).json == cancelled.json


def test_concurrent_duplicate_cancellation(populated):
    sid, _ = populated
    row = freeze(sid)
    barrier = Barrier(2)
    def worker():
        barrier.wait(5)
        return cancel(sid, row.checkout_id)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(worker), pool.submit(worker)
        assert a.result(10) == b.result(10)
    assert sql('SELECT COUNT(*) FROM checkouts') == [(1,)]


@pytest.mark.parametrize('field', ['customer_uid', 'cart_id', 'amount', 'payment_started_at', 'payment_success', 'checkout_id'])
def test_cancel_body_fields_rejected(populated, field):
    sid, _ = populated
    row = freeze(sid)
    before = state()
    assert post_cancel(api(), sid, row.checkout_id, {field: 'spoof'}).status_code == 400
    assert state() == before


@pytest.mark.parametrize('identifier', ['0', '-1', 'abc', '1.0', '9223372036854775808'])
def test_cancel_malformed_ids(populated, identifier):
    sid, _ = populated
    assert post_cancel(api(), sid, identifier).status_code == 400
    assert post_cancel(api(), identifier, 1).status_code == 400


def test_cancel_security_target_and_completed(populated):
    sid, _ = populated
    row = freeze(sid)
    url = f'/sessions/{sid}/checkouts/{row.checkout_id}/cancel'
    assert api().post(url, json={}).status_code == 401
    assert api().post(url, json={}, headers={'Authorization': 'Bearer bad'}).status_code == 403
    assert post_cancel(api('foreign'), sid, row.checkout_id).status_code == 403
    assert post_cancel(api(), sid, 9999).status_code == 404
    other = billing.create_session('CART-B', customer_uid='foreign')
    assert post_cancel(api('foreign'), other, row.checkout_id).status_code == 409
    assert post_cancel(api(), 9999, row.checkout_id).status_code == 404
    billing.complete_session(sid)
    assert post_cancel(api(), sid, row.checkout_id).status_code == 409
    assert sql('SELECT state,payment_started_at FROM checkouts') == [('COMPLETED', None)]


def test_payment_reservation_is_sticky_and_blocks_cancel(populated):
    sid, _ = populated
    row = freeze(sid)
    sql("UPDATE checkouts SET payment_started_at=datetime('now') WHERE checkout_id=?", (row.checkout_id,))
    before = state()
    response = post_cancel(api(), sid, row.checkout_id)
    assert response.status_code == 409 and response.json['error'] == 'payment_started'
    assert state() == before
    with pytest.raises(sqlite3.IntegrityError):
        sql('UPDATE checkouts SET payment_started_at=NULL')
    assert state() == before


def test_completion_preserves_exact_frozen_amount_with_existing_tolerance(populated):
    sid, _ = populated
    sql('UPDATE sessions SET total_amount=?', (20 + 1e-10,))
    row = freeze(sid)
    assert billing.complete_session(sid) == row.final_amount
    assert sql('SELECT total_amount FROM sessions') == [(row.final_amount,)]
    assert read(api(), sid).json['latest_checkout']['state'] == 'COMPLETED'


def test_completion_rejects_corrupt_frozen_bill_without_partial_history(populated):
    sid, _ = populated
    freeze(sid)
    sql('UPDATE bill_items SET total=999')
    before = state()
    with pytest.raises(checkout.CheckoutError):
        billing.complete_session(sid)
    assert state() == before


def test_owner_state_read_all_lifecycles_and_no_data_leak(populated):
    sid, _ = populated
    assert read(api('other'), sid).status_code == 403
    assert api().get(f'/sessions/{sid}/checkout').status_code == 401
    shopping = read(api(), sid).json
    assert shopping['state'] == 'SHOPPING' and shopping['latest_checkout'] is None
    row = freeze(sid)
    frozen = read(api(), sid).json
    assert frozen['state'] == 'CHECKOUT_FROZEN' and frozen['current_checkout_id'] == row.checkout_id
    assert 'request_key' not in str(frozen) and 'customer_uid' not in str(frozen)
    cancel(sid, row.checkout_id)
    cancelled = read(api(), sid).json
    assert cancelled['state'] == 'SHOPPING' and cancelled['current_checkout_id'] is None
    assert cancelled['latest_checkout']['state'] == 'CANCELLED'
    assert cancelled['runtime_restart_required']
    freeze(sid, 'second')
    billing.complete_session(sid)
    done = read(api(), sid).json
    assert done['state'] == 'COMPLETED' and done['latest_checkout']['state'] == 'COMPLETED'
    assert not done['runtime_restart_required']


def test_pairing_and_ownership_after_cancel(populated):
    sid, _ = populated
    row = freeze(sid)
    cancel(sid, row.checkout_id)
    assert pair_and_assign_in_transaction('CART-A', 'owner').session_id == sid
    for cart, owner in [('CART-A', 'other'), ('CART-B', 'owner')]:
        with pytest.raises(IntegrityConflict):
            pair_and_assign_in_transaction(cart, owner)
    for fn, value in [(billing.assign_customer_to_session, 'other'), (billing.set_session_cart_id, 'CART-B')]:
        with pytest.raises(billing.InactiveSessionError):
            fn(sid, value)
    assert sql('SELECT COUNT(*) FROM sessions') == [(1,)]


@pytest.mark.parametrize('completed', [False, True])
def test_legacy_backfill_is_atomic_repeatable_and_preserves_values(populated, completed):
    sid, _ = populated
    sql('DROP TABLE checkouts')  # emulate the real phase-one schema, no history yet
    sql("UPDATE sessions SET checkout_started_at='2026-10-03 12:00:00'")
    if completed:
        sql("UPDATE sessions SET end_time='2026-10-03 12:01:00'")
    before = sql('SELECT * FROM sessions')
    database.initialize_database()
    assert sql('SELECT * FROM sessions') == before
    assert sql('SELECT session_id,request_key,amount,created_at,state FROM checkouts') == [
        (sid, f'legacy:{sid}', 20, '2026-10-03 12:00:00', 'COMPLETED' if completed else 'FROZEN')]
    migrated = state()
    database.initialize_database()
    assert state() == migrated


@pytest.mark.parametrize('corruption', ["UPDATE sessions SET total_amount=999", 'DELETE FROM products',
                                       "UPDATE sessions SET customer_uid=NULL"])
def test_corrupt_legacy_backfill_rolls_back_new_table(populated, corruption):
    sql('DROP TABLE checkouts')
    sql("UPDATE sessions SET checkout_started_at=datetime('now')")
    sql(corruption)
    before = state()
    with pytest.raises(checkout.CheckoutError):
        database.initialize_database()
    assert state() == before
    assert sql("SELECT name FROM sqlite_master WHERE name='checkouts'") == []


def test_no_history_is_invented_and_foreign_keys_enabled(populated):
    database.initialize_database()
    assert sql('SELECT * FROM checkouts') == []
    assert sql('PRAGMA foreign_keys') == [(1,)]
    with pytest.raises(sqlite3.IntegrityError):
        sql("INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state) "
            "VALUES(999,'orphan',1,'INR',datetime('now'),'FROZEN')")
    names = {r[1] for r in sql('PRAGMA index_list(checkouts)')}
    assert {'one_frozen_checkout', 'checkout_history_order'} <= names


@pytest.mark.parametrize('amount,currency,kind,cancelled,payment', [
    (-1, 'INR', 'FROZEN', None, None),
    (1, 'USD', 'FROZEN', None, None),
    (1, 'INR', 'OTHER', None, None),
    (1, 'INR', 'CANCELLED', None, None),
    (1, 'INR', 'FROZEN', '2026-10-05 00:00:00', None),
    (1, 'INR', 'CANCELLED', '2026-10-05 00:00:00', '2026-10-05 00:00:00'),
])
def test_insert_constraints(populated, amount, currency, kind, cancelled, payment):
    sid, _ = populated
    with pytest.raises(sqlite3.IntegrityError):
        sql('INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state,cancelled_at,payment_started_at) '
            "VALUES(?,'constraint',?,?,datetime('now'),?,?,?)", (sid, amount, currency, kind, cancelled, payment))
    assert sql('SELECT * FROM checkouts') == []


def test_key_unique_even_after_cancellation_and_terminal_row_retained(populated):
    sid, _ = populated
    row = freeze(sid)
    cancel(sid, row.checkout_id)
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql("INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state) "
            "VALUES(?,'first',20,'INR',datetime('now'),'FROZEN')", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        sql("UPDATE checkouts SET cancelled_at='2026-10-05 00:00:01'")
    assert state() == before


def test_hydration_reads_contents_and_generation_in_one_transaction(populated, monkeypatch):
    sid, _ = populated
    row = freeze(sid)
    cancel(sid, row.checkout_id)
    original = billing.get_connection
    readers, statements = [], []
    def observe():
        connection = original()
        readers.append(connection)
        def trace(statement):
            statements.append(statement)
            if statement.startswith('SELECT COALESCE(MAX(checkout_id)'):
                # A second writer cannot interleave a freeze between contents
                # and generation. Real independent connection, no mocked locks.
                with closing(database.get_connection()) as writer:
                    writer.execute('PRAGMA busy_timeout=0')
                    try:
                        writer.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError:
                        statements.append('writer blocked')
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(billing, 'get_connection', observe)
    contents, generation = billing.hydrate_cart(sid)
    assert len(readers) == 1 and generation == row.checkout_id
    assert contents[0][2] == 2
    assert statements[0] == 'BEGIN IMMEDIATE' and statements[-1] == 'COMMIT'
    assert 'writer blocked' in statements
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        readers[0].execute('SELECT 1')


@pytest.mark.parametrize('statement', [
    'UPDATE checkouts SET amount=99', "UPDATE checkouts SET request_key='other'",
    'DELETE FROM checkouts', 'DELETE FROM sessions',
    "UPDATE checkouts SET state='CANCELLED'", "UPDATE checkouts SET state='OTHER'",
    "UPDATE checkouts SET currency='USD'",
    "INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state) "
    "SELECT session_id,'other',amount,currency,created_at,state FROM checkouts",
])
def test_sql_constraints_and_immutable_history(populated, statement):
    sid, _ = populated
    freeze(sid)
    before = state()
    with pytest.raises(sqlite3.IntegrityError):
        sql(statement)
    assert state() == before


@pytest.mark.parametrize('operation', ['read', 'cancel', 'freeze', 'add', 'initialize'])
def test_marker_history_mismatch_fails_closed(populated, operation):
    sid, pid = populated
    row = freeze(sid)
    sql('UPDATE sessions SET checkout_started_at=NULL')
    before = state()
    actions = dict(read=lambda: checkout.read_checkout_state(sid, 'owner'),
                   cancel=lambda: cancel(sid, row.checkout_id), freeze=lambda: freeze(sid),
                   add=lambda: billing.add_item(sid, pid), initialize=database.initialize_database)
    with pytest.raises(checkout.CheckoutError):
        actions[operation]()
    assert state() == before


def test_cancel_rollback_and_lock_timeout_keep_both_representations(populated, monkeypatch):
    sid, _ = populated
    row = freeze(sid)
    sql("CREATE TRIGGER fail_clear BEFORE UPDATE OF checkout_started_at ON sessions "
        "BEGIN SELECT RAISE(ABORT,'private detail'); END")
    before = state()
    response = post_cancel(api(), sid, row.checkout_id)
    assert response.status_code == 503 and 'private' not in response.get_data(as_text=True)
    assert state() == before
    sql('DROP TRIGGER fail_clear')
    original = checkout.get_connection
    def impatient():
        conn = original()
        conn.execute('PRAGMA busy_timeout=0')
        return conn
    monkeypatch.setattr(checkout, 'get_connection', impatient)
    before = state()
    with closing(database.get_connection()) as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert post_cancel(api(), sid, row.checkout_id).status_code == 503
        writer.rollback()
    assert state() == before


@pytest.mark.parametrize('mutation', ['add_item', 'remove_item'])
@pytest.mark.parametrize('transition', ['cancel', 'refreeze'])
@pytest.mark.parametrize('first', ['transition', 'mutation'])
def test_transactions_serialize_cancel_and_refreeze_races(populated, monkeypatch, mutation, transition, first):
    sid, pid = populated
    row = freeze(sid)
    if transition == 'refreeze':
        cancel(sid, row.checkout_id)
    # A generation-bound trusted writer; physical resume authorization remains
    # an operator procedure. Old generation zero is tested separately.
    do_mutation = lambda: getattr(billing, mutation)(sid, pid, expected_generation=row.checkout_id)
    do_transition = lambda: cancel(sid, row.checkout_id) if transition == 'cancel' else freeze(sid, 'second')
    held, release, attempted = Event(), Event(), Event()
    first_module = checkout if first == 'transition' else billing
    second_module = billing if first == 'transition' else checkout
    first_connection, second_connection = first_module.get_connection, second_module.get_connection
    def holder():
        conn = first_connection()
        def trace(statement):
            # Hold the transaction even for a rejected frozen mutation.
            if statement in ('COMMIT', 'ROLLBACK'):
                held.set()
                release.wait(5)
        conn.set_trace_callback(trace)
        return conn
    def contender():
        conn = second_connection()
        conn.set_trace_callback(lambda stmt: attempted.set() if stmt == 'BEGIN IMMEDIATE' else None)
        return conn
    monkeypatch.setattr(first_module, 'get_connection', holder)
    monkeypatch.setattr(second_module, 'get_connection', contender)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(do_transition if first == 'transition' else do_mutation)
        assert held.wait(5)
        b = pool.submit(do_mutation if first == 'transition' else do_transition)
        try:
            assert attempted.wait(5)
        finally:
            release.set()
        mutation_future, transition_future = (b, a) if first == 'transition' else (a, b)
        rejected = (transition == 'cancel' and first == 'mutation') or (transition == 'refreeze' and first == 'transition')
        if rejected:
            with pytest.raises(billing.CheckoutFrozenError):
                mutation_future.result(10)
        else:
            assert mutation_future.result(10)
        transition_future.result(10)
    expected = 20 if rejected else 30 if mutation == 'add_item' else 10
    assert billing.calculate_total(sid) == expected
