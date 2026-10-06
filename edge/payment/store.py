"""SQLite payment authority. Every mutation uses one short writer transaction."""
from contextlib import contextmanager
import sqlite3
from uuid import uuid4

from edge.database.database import get_connection
from edge.billing.checkout import (_owned_session, _validate_id, _validate_bill,
                                   _unique_owner, checkout_history)
from .money import PaymentError, to_paise, DEFAULT_MAX_PAISE, MAX_SAFE_PAISE


TRANSITIONS = {
    'RESERVED': ('CREATING',),
    'CREATING': ('ORDER_READY', 'FAILED'),
    'ORDER_READY': ('VERIFIED',),
    'VERIFIED': ('COMPLETED',),
    'COMPLETED': (),
    'FAILED': (),
}


def integrity():
    raise PaymentError('payment_integrity', 'Payment requires backend inspection.')


def _one(conn, query, args=()):
    cursor = conn.execute(query, args)
    row = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), row)) if row else None


def _attempt(conn, attempt_id):
    if type(attempt_id) is not str or not 1 <= len(attempt_id) <= 128:
        raise PaymentError('invalid_payment_id', 'Invalid payment identity.', 400)
    result = _one(conn, 'SELECT * FROM payment_attempts WHERE payment_attempt_id=?', (attempt_id,))
    if result is None:
        raise PaymentError('payment_not_found', 'Payment not found.', 404)
    return result


def reservation_consistent(checkout, attempt):
    """Marker and snapshot are a compatibility projection of one durable attempt."""
    if attempt is None:
        if checkout['payment_started_at'] is not None or checkout['amount_paise'] is not None:
            integrity()
        return
    if (checkout['checkout_id'] != attempt['checkout_id']
            or checkout['payment_started_at'] != attempt['created_at']
            or type(checkout['amount_paise']) is not int
            or checkout['amount_paise'] != attempt['amount_paise']
            or checkout['currency'] != attempt['currency']
            or checkout['state'] == 'CANCELLED'
            or (checkout['state'] == 'COMPLETED') != (attempt['state'] == 'COMPLETED')):
        integrity()


def migrate_payments(conn):
    """Runs inside the initializer's existing atomic checkout migration."""
    if 'amount_paise' not in {r[1] for r in conn.execute('PRAGMA table_info(checkouts)')}:
        conn.execute(f'''ALTER TABLE checkouts ADD COLUMN amount_paise INTEGER
            CHECK(amount_paise IS NULL OR
                  (typeof(amount_paise)='integer' AND amount_paise BETWEEN 1 AND {MAX_SAFE_PAISE}))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS payment_attempts (
        payment_attempt_id TEXT PRIMARY KEY NOT NULL CHECK(length(payment_attempt_id) BETWEEN 1 AND 128),
        checkout_id INTEGER NOT NULL UNIQUE REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
        request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 128),
        provider_scope TEXT NOT NULL CHECK(length(provider_scope) BETWEEN 1 AND 128),
        amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 1 AND 9007199254740991),
        currency TEXT NOT NULL CHECK(currency='INR'),
        state TEXT NOT NULL CHECK(state IN ('RESERVED','CREATING','ORDER_READY','VERIFIED','COMPLETED','FAILED')),
        provider_order_id TEXT CHECK(provider_order_id IS NULL OR length(provider_order_id) BETWEEN 1 AND 256),
        provider_payment_id TEXT CHECK(provider_payment_id IS NULL OR length(provider_payment_id) BETWEEN 1 AND 256),
        created_at TEXT NOT NULL CHECK(datetime(created_at) IS NOT NULL AND datetime(created_at)=created_at),
        updated_at TEXT NOT NULL CHECK(datetime(updated_at) IS NOT NULL AND datetime(updated_at)=updated_at AND updated_at>=created_at),
        verified_at TEXT CHECK(verified_at IS NULL OR (datetime(verified_at) IS NOT NULL AND datetime(verified_at)=verified_at AND verified_at>=created_at AND verified_at<=updated_at)),
        completed_at TEXT CHECK(completed_at IS NULL OR (datetime(completed_at) IS NOT NULL AND datetime(completed_at)=completed_at AND completed_at>=verified_at AND completed_at<=updated_at)),
        error_code TEXT CHECK(error_code IS NULL OR error_code IN ('provider_rejected','recovery_required')),
        UNIQUE(provider_scope, provider_order_id),
        UNIQUE(provider_scope, provider_payment_id),
        CHECK((state IN ('RESERVED','CREATING','FAILED') AND provider_order_id IS NULL)
           OR (state IN ('ORDER_READY','VERIFIED','COMPLETED') AND provider_order_id IS NOT NULL)),
        CHECK((state IN ('VERIFIED','COMPLETED') AND provider_payment_id IS NOT NULL AND verified_at IS NOT NULL)
           OR (state NOT IN ('VERIFIED','COMPLETED') AND provider_payment_id IS NULL AND verified_at IS NULL)),
        CHECK((state='COMPLETED' AND completed_at IS NOT NULL) OR (state!='COMPLETED' AND completed_at IS NULL)),
        CHECK((state='FAILED' AND error_code IS NOT NULL AND error_code='provider_rejected')
           OR (state='CREATING' AND (error_code IS NULL OR error_code='recovery_required'))
           OR (state NOT IN ('FAILED','CREATING') AND error_code IS NULL))
    )''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS checkout_paise_once BEFORE UPDATE OF amount_paise ON checkouts
        WHEN OLD.amount_paise IS NOT NULL OR NEW.amount_paise IS NULL OR OLD.state!='FROZEN'
          OR NEW.payment_started_at IS NULL
          OR EXISTS(SELECT 1 FROM payment_attempts WHERE checkout_id=OLD.checkout_id)
        BEGIN SELECT RAISE(ABORT,'Payment snapshot is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS checkout_paise_insert BEFORE INSERT ON checkouts
        WHEN NEW.amount_paise IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Payment snapshot requires reservation'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS payment_insert_guard BEFORE INSERT ON payment_attempts
        WHEN NEW.state!='RESERVED' OR EXISTS(SELECT 1 FROM payment_attempts
            WHERE checkout_id=NEW.checkout_id OR payment_attempt_id=NEW.payment_attempt_id)
          OR NOT EXISTS(
            SELECT 1 FROM checkouts c JOIN sessions s ON s.session_id=c.session_id
            WHERE c.checkout_id=NEW.checkout_id AND c.state='FROZEN' AND s.end_time IS NULL
              AND c.checkout_id=(SELECT MAX(checkout_id) FROM checkouts WHERE session_id=s.session_id)
              AND c.amount_paise=NEW.amount_paise AND c.currency=NEW.currency
              AND c.payment_started_at=NEW.created_at)
        BEGIN SELECT RAISE(ABORT,'Payment requires exact reservation'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS payment_identity_immutable
        BEFORE UPDATE OF payment_attempt_id,checkout_id,request_key,provider_scope,amount_paise,currency,created_at ON payment_attempts
        BEGIN SELECT RAISE(ABORT,'Payment identity is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS payment_history_retained BEFORE DELETE ON payment_attempts
        BEGIN SELECT RAISE(ABORT,'Payment history must be retained'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS payment_identifiers_sticky BEFORE UPDATE ON payment_attempts
        WHEN (OLD.provider_order_id IS NOT NULL AND NEW.provider_order_id IS NOT OLD.provider_order_id)
          OR (OLD.provider_payment_id IS NOT NULL AND NEW.provider_payment_id IS NOT OLD.provider_payment_id)
          OR (OLD.verified_at IS NOT NULL AND NEW.verified_at IS NOT OLD.verified_at)
          OR NEW.updated_at<OLD.updated_at
        BEGIN SELECT RAISE(ABORT,'Payment evidence is immutable'); END''')
    allowed = ' OR '.join(f"(OLD.state='{old}' AND NEW.state='{new}')"
                          for old, targets in TRANSITIONS.items() for new in targets)
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS payment_transition BEFORE UPDATE ON payment_attempts
        WHEN OLD.state IN ('COMPLETED','FAILED') OR
          (NEW.state!=OLD.state AND NOT ({allowed}))
        BEGIN SELECT RAISE(ABORT,'Invalid payment transition'); END''')
    for row in conn.execute('SELECT checkout_id FROM checkouts').fetchall():
        checkout = _one(conn, 'SELECT * FROM checkouts WHERE checkout_id=?', row)
        attempt = _one(conn, 'SELECT * FROM payment_attempts WHERE checkout_id=?', row)
        reservation_consistent(checkout, attempt)
    if conn.execute('PRAGMA foreign_key_check(payment_attempts)').fetchone():
        integrity()


def _key(value):
    if (type(value) is not str or not 1 <= len(value) <= 128
            or any(not 33 <= ord(c) <= 126 for c in value)):
        raise PaymentError('invalid_idempotency_key', 'A payment Idempotency-Key is required.', 400)


def _checkout(rows, checkout_id):
    _validate_id(checkout_id)
    row = next((r for r in rows if r['checkout_id'] == checkout_id), None)
    if row is None:
        raise PaymentError('checkout_not_found', 'Checkout not found in this session.', 404)
    return row


def _current(conn, checkout, rows, end):
    if end is not None or checkout['state'] != 'FROZEN' or checkout != rows[-1]:
        raise PaymentError('checkout_not_payable', 'Exact checkout is not current and frozen.')
    _validate_bill(conn, checkout['session_id'], checkout['amount'])


def begin_payment(session_id, checkout_id, verified_customer_uid, request_key, *,
                  provider_scope, max_paise=DEFAULT_MAX_PAISE):
    _key(request_key)
    _key(provider_scope)
    with _owned_session(session_id, verified_customer_uid) as (conn, cart, end, amount, rows):
        checkout = _checkout(rows, checkout_id)
        attempt = _one(conn, 'SELECT * FROM payment_attempts WHERE checkout_id=?', (checkout_id,))
        reservation_consistent(checkout, attempt)
        if attempt:
            if attempt['request_key'] != request_key:
                raise PaymentError('payment_key_conflict', 'This checkout already has a payment attempt.')
            if attempt['provider_scope'] != provider_scope:
                integrity()
            # Lost successful response: the exact completed attempt is repeatable.
            if attempt['state'] != 'COMPLETED':
                _current(conn, checkout, rows, end)
            return attempt
        _current(conn, checkout, rows, end)
        paise = to_paise(checkout['amount'], max_paise=max_paise)
        if paise == 0:
            raise PaymentError('zero_payment', 'Zero-total payment is not supported.')
        now = conn.execute("SELECT datetime('now')").fetchone()[0]
        attempt_id = uuid4().hex
        conn.execute('UPDATE checkouts SET amount_paise=?,payment_started_at=? WHERE checkout_id=?',
                     (paise, now, checkout_id))
        conn.execute('''INSERT INTO payment_attempts(payment_attempt_id,checkout_id,request_key,provider_scope,
                     amount_paise,currency,state,created_at,updated_at) VALUES (?,?,?,?,?,'INR','RESERVED',?,?)''',
                     (attempt_id, checkout_id, request_key, provider_scope, paise, now, now))
        return _attempt(conn, attempt_id)


def read_payment(session_id, checkout_id, verified_customer_uid):
    with _owned_session(session_id, verified_customer_uid, write=False) as (conn, cart, end, amount, rows):
        checkout = _checkout(rows, checkout_id)
        attempt = _one(conn, 'SELECT * FROM payment_attempts WHERE checkout_id=?', (checkout_id,))
        reservation_consistent(checkout, attempt)
        if attempt is None:
            raise PaymentError('payment_not_found', 'Payment not found.', 404)
        return attempt


@contextmanager
def _transaction(attempt_id):
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        attempt = _attempt(conn, attempt_id)
        checkout = _one(conn, 'SELECT * FROM checkouts WHERE checkout_id=?', (attempt['checkout_id'],))
        if checkout is None:
            integrity()
        session = _one(conn, 'SELECT * FROM sessions WHERE session_id=?', (checkout['session_id'],))
        if session is None:
            integrity()
        rows = checkout_history(conn, session['session_id'], session['end_time'], session['checkout_started_at'])
        reservation_consistent(checkout, attempt)
        if attempt['state'] != 'COMPLETED':
            _unique_owner(conn, session['session_id'], session['cart_id'], session['customer_uid'])
            _current(conn, checkout, rows, session['end_time'])
        yield conn, attempt, checkout, session
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise PaymentError('payment_conflict', 'Payment identity or state conflicts with persisted history.') from None
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _transition(conn, attempt, state, **fields):
    if state != attempt['state'] and state not in TRANSITIONS[attempt['state']]:
        integrity()
    if not set(fields) <= {'provider_order_id', 'provider_payment_id', 'verified_at', 'completed_at', 'error_code'}:
        raise ValueError('Unsupported payment transition field')
    assignments = ','.join(f'{name}=?' for name in fields)
    conn.execute("UPDATE payment_attempts SET state=?,updated_at=MAX(updated_at,datetime('now'))"
                 + (',' + assignments if assignments else '') + ' WHERE payment_attempt_id=?',
                 (state, *fields.values(), attempt['payment_attempt_id']))
    return _attempt(conn, attempt['payment_attempt_id'])


def claim_dispatch(attempt_id):
    with _transaction(attempt_id) as (conn, attempt, checkout, session):
        if attempt['state'] != 'RESERVED':
            return attempt, False
        return _transition(conn, attempt, 'CREATING'), True


def _matches(attempt, result):
    if (result.payment_attempt_id != attempt['payment_attempt_id']
            or type(result.checkout_id) is not int or result.checkout_id != attempt['checkout_id']
            or result.provider_scope != attempt['provider_scope']
            or type(result.amount_paise) is not int or result.amount_paise != attempt['amount_paise']
            or result.currency != attempt['currency']
            or type(result.order_id) is not str or not 1 <= len(result.order_id) <= 256):
        raise PaymentError('payment_evidence_mismatch', 'Provider evidence does not match the exact checkout.')


def save_order(attempt_id, order):
    with _transaction(attempt_id) as (conn, attempt, checkout, session):
        _matches(attempt, order)
        if attempt['provider_order_id'] is not None:
            if attempt['provider_order_id'] != order.order_id:
                integrity()
            return attempt
        if attempt['state'] != 'CREATING':
            integrity()
        return _transition(conn, attempt, 'ORDER_READY', provider_order_id=order.order_id, error_code=None)


def creation_problem(attempt_id, *, rejected=False):
    with _transaction(attempt_id) as (conn, attempt, checkout, session):
        if attempt['state'] != 'CREATING':
            return attempt  # A concurrent verified result must never be downgraded.
        return _transition(conn, attempt, 'FAILED' if rejected else 'CREATING',
                           error_code='provider_rejected' if rejected else 'recovery_required')


def save_verified(attempt_id, payment):
    """Internal adapter-result sink; never exposed as a customer authorization API."""
    with _transaction(attempt_id) as (conn, attempt, checkout, session):
        _matches(attempt, payment)
        if (attempt['provider_order_id'] != payment.order_id
                or type(payment.payment_id) is not str or not 1 <= len(payment.payment_id) <= 256):
            raise PaymentError('payment_evidence_mismatch', 'Provider payment identity does not match.')
        if payment.status != 'CAPTURED':
            raise PaymentError('payment_not_successful', 'Provider has not confirmed successful payment.')
        if attempt['state'] in ('VERIFIED', 'COMPLETED'):
            if attempt['provider_payment_id'] != payment.payment_id:
                raise PaymentError('payment_conflict', 'A different payment is already recorded.')
            return attempt
        if attempt['state'] != 'ORDER_READY':
            integrity()
        now = conn.execute("SELECT MAX(?,datetime('now'))", (attempt['updated_at'],)).fetchone()[0]
        return _transition(conn, attempt, 'VERIFIED', provider_payment_id=payment.payment_id, verified_at=now)


def complete_paid_checkout(checkout_id, payment_attempt_id):
    """Consume persisted verification and complete exact checkout/session atomically.

    Returns (public result, newly_completed). No external I/O is performed here.
    """
    _validate_id(checkout_id)
    with _transaction(payment_attempt_id) as (conn, attempt, checkout, session):
        if attempt['checkout_id'] != checkout_id:
            raise PaymentError('payment_checkout_mismatch', 'Payment belongs to a different checkout.')
        if attempt['state'] not in ('VERIFIED', 'COMPLETED'):
            raise PaymentError('payment_not_verified', 'Payment has not been independently verified.')
        if not all(attempt[k] for k in ('provider_scope', 'provider_order_id', 'provider_payment_id', 'verified_at')):
            integrity()
        _validate_bill(conn, session['session_id'], checkout['amount'])
        fresh = attempt['state'] == 'VERIFIED'
        if fresh:
            now = conn.execute("SELECT MAX(?,datetime('now'))", (attempt['updated_at'],)).fetchone()[0]
            conn.execute("UPDATE checkouts SET state='COMPLETED' WHERE checkout_id=?", (checkout_id,))
            attempt = _transition(conn, attempt, 'COMPLETED', completed_at=now)
            conn.execute('UPDATE sessions SET end_time=? WHERE session_id=?', (now, session['session_id']))
        elif session['end_time'] != attempt['completed_at']:
            integrity()
        return dict(session_id=session['session_id'], checkout_id=checkout_id,
                    payment_attempt_id=attempt['payment_attempt_id'], state='COMPLETED',
                    amount_paise=attempt['amount_paise'], currency=attempt['currency'],
                    completed_at=attempt['completed_at']), fresh
