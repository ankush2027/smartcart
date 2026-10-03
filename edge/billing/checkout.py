"""Authenticated, irreversible checkout freeze; no network or payment operations."""
from dataclasses import dataclass
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


def begin_checkout(session_id, verified_customer_uid) -> CheckoutResult:
    """Serialize freeze against billing writers in the same SQLite database.

    UID must come from the trusted token verifier. Session ID is the retry
    identity; never redirect an old request to another session. Completion
    remains a separate trusted operation. Frozen retries validate but never
    reprice or rewrite the stored amount/timestamp.
    """
    try:
        validate_session_id(session_id)
    except ProjectionError:
        raise CheckoutError('invalid_session_id', 'Invalid session ID.', 400) from None
    if not isinstance(verified_customer_uid, str) or not verified_customer_uid.strip():
        raise CheckoutError('authentication_required', 'Verified customer required.', 401)
    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            'SELECT cart_id, customer_uid, end_time, total_amount, checkout_started_at '
            'FROM sessions WHERE session_id = ?', (session_id,),
        ).fetchone()
        if row is None:
            raise CheckoutError('session_not_found', 'Session not found.', 404)
        cart, owner, end, amount, started = row
        if owner != verified_customer_uid:
            raise CheckoutError('session_forbidden', 'Session is not owned by this customer.', 403)
        if end is not None:
            raise CheckoutError('session_completed', 'Session is already completed.')
        if not isinstance(cart, str) or not cart.strip():
            raise CheckoutError('invalid_session', 'Session has no valid cart assignment.')
        for column, value in (('cart_id', cart), ('customer_uid', owner)):
            ids = connection.execute(
                f'SELECT session_id FROM sessions WHERE {column} = ? AND end_time IS NULL',
                (value,),
            ).fetchall()
            if ids != [(session_id,)]:
                raise CheckoutError('session_conflict', 'Active session ownership is inconsistent.')
        _validate_bill(connection, session_id, amount)
        if started is None:
            connection.execute(
                "UPDATE sessions SET checkout_started_at = datetime('now') WHERE session_id = ?",
                (session_id,),
            )
            started = connection.execute(
                'SELECT checkout_started_at FROM sessions WHERE session_id = ?', (session_id,),
            ).fetchone()[0]
        else:
            try:
                datetime.strptime(started, '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                raise CheckoutError('invalid_session', 'Invalid checkout timestamp.') from None
        result = CheckoutResult(session_id, cart, started, amount)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
