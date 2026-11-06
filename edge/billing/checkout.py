"""Durable checkout identities and exact-ID cancellation; no network/payment I/O."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import math

from edge.database.database import get_connection
from edge.billing.session_projection import validate_session_id, ProjectionError


class CheckoutError(ValueError):
    def __init__(self, code, message, http_status=409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


@dataclass(frozen=True)
class CheckoutResult:
    session_id: int
    cart_id: str
    checkout_started_at: str
    final_amount: float
    checkout_id: int
    state: str
    cancelled_at: str | None
    runtime_restart_required: bool
    currency: str = 'INR'


def _invalid_bill():
    raise CheckoutError('invalid_bill', 'Authoritative bill requires backend inspection.')


def _validate_bill(connection, session_id, maintained_total):
    rows = connection.execute(
        'SELECT b.product_id, p.name, b.quantity, b.price, b.total '
        'FROM bill_items b LEFT JOIN products p ON p.product_id = b.product_id '
        'WHERE b.session_id = ? ORDER BY b.product_id', (session_id,),
    ).fetchall()
    if not rows:
        raise CheckoutError('empty_cart', 'An empty cart cannot enter checkout.')
    seen, totals = set(), []
    for pid, name, quantity, price, total in rows:
        if (type(pid) is not int or pid <= 0 or pid in seen
                or not isinstance(name, str) or not name.strip()
                or type(quantity) is not int or quantity <= 0):
            _invalid_bill()
        seen.add(pid)
        for value in (price, total, maintained_total):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                _invalid_bill()
        if not math.isclose(price * quantity, total, rel_tol=1e-9, abs_tol=1e-9):
            _invalid_bill()
        totals.append(total)
    try:
        total = math.fsum(totals)
    except OverflowError:
        _invalid_bill()
    if not math.isclose(total, maintained_total, rel_tol=1e-9, abs_tol=1e-9):
        _invalid_bill()


def _validate_id(value):
    try:
        validate_session_id(value)
    except ProjectionError:
        raise CheckoutError('invalid_session_id', 'Invalid session ID.', 400) from None


def _timestamp(value):
    try:
        datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        raise CheckoutError('invalid_session', 'Invalid checkout timestamp.') from None


def _integrity():
    raise CheckoutError('checkout_integrity', 'Checkout lifecycle requires backend inspection.')


def checkout_history(connection, session_id, end_time, marker):
    """Validate both lifecycle representations on the caller's transaction.

    History is never deleted, so the greatest ID is also the runtime generation.
    Cancelled amounts are independent of today's mutable session bill.
    """
    if marker is not None:
        _timestamp(marker)
    cursor = connection.execute('SELECT * FROM checkouts WHERE session_id = ? ORDER BY checkout_id',
                                (session_id,))
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    for row in rows:
        amount = row['amount']
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            _integrity()
        _timestamp(row['created_at'])
        if row['cancelled_at'] is not None:
            _timestamp(row['cancelled_at'])
        if row['payment_started_at'] is not None:
            _timestamp(row['payment_started_at'])
        if row['state'] not in ('FROZEN', 'CANCELLED', 'COMPLETED') or row['currency'] != 'INR':
            _integrity()
        if ((row['state'] == 'CANCELLED') != (row['cancelled_at'] is not None)
                or (row['state'] == 'CANCELLED' and row['payment_started_at'] is not None)):
            _integrity()
    latest = rows[-1] if rows else None
    if any(row['state'] != 'CANCELLED' for row in rows[:-1]):
        _integrity()
    if latest and latest['state'] in ('FROZEN', 'COMPLETED'):
        if marker != latest['created_at'] or (latest['state'] == 'COMPLETED') != (end_time is not None):
            _integrity()
        amount = connection.execute('SELECT total_amount FROM sessions WHERE session_id = ?',
                                    (session_id,)).fetchone()[0]
        if amount != latest['amount']:
            _invalid_bill()
    elif marker is not None:
        _integrity()
    return rows


def migrate_checkouts(connection):
    """Called only by initialization inside an explicit writer transaction."""
    existed = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkouts'"
    ).fetchone() is not None
    connection.execute('''CREATE TABLE IF NOT EXISTS checkouts (
        checkout_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
        request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 128),
        amount REAL NOT NULL CHECK(amount >= 0),
        currency TEXT NOT NULL CHECK(currency = 'INR'),
        created_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('FROZEN','CANCELLED','COMPLETED')),
        cancelled_at TEXT,
        payment_started_at TEXT,
        UNIQUE(session_id, request_key),
        CHECK((state = 'CANCELLED' AND cancelled_at IS NOT NULL AND payment_started_at IS NULL)
           OR (state != 'CANCELLED' AND cancelled_at IS NULL))
    )''')
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_frozen_checkout ON checkouts(session_id) WHERE state = 'FROZEN'")
    connection.execute('CREATE INDEX IF NOT EXISTS checkout_history_order ON checkouts(session_id, checkout_id)')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS checkout_snapshot_immutable
        BEFORE UPDATE OF checkout_id, session_id, request_key, amount, currency, created_at ON checkouts
        BEGIN SELECT RAISE(ABORT, 'Checkout snapshot is immutable'); END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS checkout_history_retained
        BEFORE DELETE ON checkouts
        BEGIN SELECT RAISE(ABORT, 'Checkout history must be retained'); END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS checkout_terminal_immutable
        BEFORE UPDATE ON checkouts WHEN OLD.state != 'FROZEN'
        BEGIN SELECT RAISE(ABORT, 'Terminal checkout is immutable'); END''')
    connection.execute('''CREATE TRIGGER IF NOT EXISTS checkout_payment_sticky
        BEFORE UPDATE OF payment_started_at ON checkouts
        WHEN OLD.payment_started_at IS NOT NULL AND NEW.payment_started_at IS NOT OLD.payment_started_at
        BEGIN SELECT RAISE(ABORT, 'Payment reservation is sticky'); END''')
    for sid, cart, owner, end, amount, marker in connection.execute(
            'SELECT session_id, cart_id, customer_uid, end_time, total_amount, checkout_started_at FROM sessions').fetchall():
        if not existed and marker is not None:
            if not all(isinstance(v, str) and v.strip() for v in (cart, owner)):
                _integrity()
            _timestamp(marker)
            _validate_bill(connection, sid, amount)
            if end is not None:
                _timestamp(end)
            else:
                _unique_owner(connection, sid, cart, owner)
            connection.execute(
                'INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state) VALUES (?,?,?,?,?,?)',
                (sid, f'legacy:{sid}', amount, 'INR', marker, 'COMPLETED' if end else 'FROZEN'))
        rows = checkout_history(connection, sid, end, marker)
        if rows:
            if not all(isinstance(v, str) and v.strip() for v in (cart, owner)):
                _integrity()
            if end is None:
                _unique_owner(connection, sid, cart, owner)
        if rows and rows[-1]['state'] in ('FROZEN', 'COMPLETED'):
            _validate_bill(connection, sid, amount)
    if connection.execute('PRAGMA foreign_key_check(checkouts)').fetchone() is not None:
        _integrity()


def _unique_owner(connection, sid, cart, owner):
    for column, value in (('cart_id', cart), ('customer_uid', owner)):
        ids = connection.execute(
            f'SELECT session_id FROM sessions WHERE {column} = ? AND end_time IS NULL', (value,)
        ).fetchall()
        if ids != [(sid,)]:
            raise CheckoutError('session_conflict', 'Active session ownership is inconsistent.')


@contextmanager
def _owned_session(session_id, verified_customer_uid, *, write=True):
    _validate_id(session_id)
    if not isinstance(verified_customer_uid, str) or not verified_customer_uid.strip():
        raise CheckoutError('authentication_required', 'Verified customer required.', 401)
    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        row = connection.execute(
            'SELECT cart_id, customer_uid, end_time, total_amount, checkout_started_at '
            'FROM sessions WHERE session_id = ?', (session_id,),
        ).fetchone()
        if row is None:
            raise CheckoutError('session_not_found', 'Session not found.', 404)
        cart, owner, end, amount, started = row
        if owner != verified_customer_uid:
            raise CheckoutError('session_forbidden', 'Session is not owned by this customer.', 403)
        if not isinstance(cart, str) or not cart.strip():
            raise CheckoutError('invalid_session', 'Session has no valid cart assignment.')
        if end is None:
            _unique_owner(connection, session_id, cart, owner)
        rows = checkout_history(connection, session_id, end, started)
        yield connection, cart, end, amount, rows
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _result(row, cart):
    return CheckoutResult(row['session_id'], cart, row['created_at'], row['amount'],
                          row['checkout_id'], row['state'], row['cancelled_at'],
                          row['state'] == 'CANCELLED')


def _unfinished(end):
    if end is not None:
        raise CheckoutError('session_completed', 'Session is already completed.')


def begin_checkout(session_id, verified_customer_uid, request_key) -> CheckoutResult:
    """A request key identifies one immutable freeze, including after cancellation."""
    _validate_id(session_id)
    if (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128
            or any(not 33 <= ord(c) <= 126 for c in request_key)
            or request_key.startswith('legacy:')):
        raise CheckoutError('invalid_idempotency_key', 'A valid Idempotency-Key is required.', 400)
    with _owned_session(session_id, verified_customer_uid) as (conn, cart, end, amount, rows):
        _unfinished(end)
        for row in rows:
            if row['request_key'] == request_key:
                if row['state'] == 'FROZEN':
                    _validate_bill(conn, session_id, amount)
                return _result(row, cart)
        if rows and rows[-1]['state'] == 'FROZEN':
            raise CheckoutError('checkout_already_frozen', 'Another checkout is already frozen.')
        _validate_bill(conn, session_id, amount)
        checkout_id = conn.execute("INSERT INTO checkouts(session_id,request_key,amount,currency,created_at,state) "
                                   "VALUES (?,?,?,'INR',datetime('now'),'FROZEN')",
                                   (session_id, request_key, amount)).lastrowid
        conn.execute('UPDATE sessions SET checkout_started_at = '
                     '(SELECT created_at FROM checkouts WHERE checkout_id = ?) '
                     'WHERE session_id = ?', (checkout_id, session_id))
        return _result(checkout_history(conn, session_id, None,
                       conn.execute('SELECT checkout_started_at FROM sessions WHERE session_id=?',
                                    (session_id,)).fetchone()[0])[-1], cart)


def cancel_checkout(session_id, checkout_id, verified_customer_uid) -> CheckoutResult:
    _validate_id(checkout_id)
    with _owned_session(session_id, verified_customer_uid) as (conn, cart, end, amount, rows):
        _unfinished(end)
        row = next((r for r in rows if r['checkout_id'] == checkout_id), None)
        if row is None:
            if conn.execute('SELECT 1 FROM checkouts WHERE checkout_id=?', (checkout_id,)).fetchone():
                raise CheckoutError('checkout_session_mismatch', 'Checkout does not belong to this session.')
            raise CheckoutError('checkout_not_found', 'Checkout not found.', 404)
        from edge.payment.store import _one, reservation_consistent
        attempt = _one(conn, 'SELECT * FROM payment_attempts WHERE checkout_id=?', (checkout_id,))
        if attempt is not None:
            reservation_consistent(row, attempt)
        if row['payment_started_at'] is not None or attempt is not None:
            raise CheckoutError('payment_started', 'Payment reservation prevents cancellation.')
        reservation_consistent(row, attempt)
        if row['state'] == 'CANCELLED':
            return _result(row, cart)
        if row != rows[-1] or row['state'] != 'FROZEN':
            _integrity()
        conn.execute("UPDATE checkouts SET state='CANCELLED', cancelled_at=datetime('now') WHERE checkout_id=?",
                     (checkout_id,))
        conn.execute('UPDATE sessions SET checkout_started_at=NULL WHERE session_id=?', (session_id,))
        return _result(checkout_history(conn, session_id, None, None)[-1], cart)


def read_checkout_state(session_id, verified_customer_uid):
    """One consistent read snapshot; historical cancellation never claims currentness."""
    with _owned_session(session_id, verified_customer_uid, write=False) as (conn, cart, end, amount, rows):
        latest = rows[-1] if rows else None
        frozen = latest is not None and latest['state'] == 'FROZEN'
        from edge.payment.money import to_paise
        items = [dict(product_id=pid, name=name, quantity=quantity,
                      unit_price_paise=to_paise(price), total_paise=to_paise(total))
                 for pid, name, quantity, price, total in conn.execute(
                     'SELECT b.product_id,p.name,b.quantity,b.price,b.total '
                     'FROM bill_items b JOIN products p ON p.product_id=b.product_id '
                     'WHERE b.session_id=? ORDER BY b.product_id', (session_id,))]
        return dict(session_id=session_id, cart_id=cart, items=items,
                    state='COMPLETED' if end is not None else 'CHECKOUT_FROZEN' if frozen else 'SHOPPING',
                    current_checkout_id=latest['checkout_id'] if frozen else None,
                    latest_checkout=asdict(_result(latest, cart)) if latest else None,
                    runtime_restart_required=end is None and latest is not None and latest['state'] == 'CANCELLED')
