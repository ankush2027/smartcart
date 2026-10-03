"""Validated, read-only SQLite snapshots for controlled projection recovery."""
from dataclasses import dataclass
from datetime import datetime
import math
import sqlite3

from edge.database import database


class ProjectionError(ValueError):
    """The authoritative session cannot safely be projected."""


@dataclass(frozen=True)
class BillItemProjection:
    product_id: int
    product_name: str
    quantity: int
    price: float
    total: float


@dataclass(frozen=True)
class SessionProjection:
    session_id: int
    cart_id: str | None
    customer_uid: str | None
    start_time: str
    end_time: str | None
    total_amount: float
    bill_items: tuple[BillItemProjection, ...]


def validate_session_id(session_id):
    if type(session_id) is not int or not 0 < session_id <= 9223372036854775807:
        raise ProjectionError('Session ID must be a positive SQLite integer')


def _money(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ProjectionError('Invalid monetary value in authoritative session')


def _timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        raise ProjectionError('Invalid session timestamp') from None


def read_session_projection(session_id: int) -> SessionProjection:
    """Read metadata and every bill line in one deferred read transaction.

    mode=ro prevents writes and accidental database creation. LEFT JOIN exposes
    missing products rather than silently omitting bill lines. Product names
    use the current catalog, matching billing.get_bill(); prices are bill values.
    The connection is always closed before the immutable snapshot is returned.
    This snapshot does not prevent later writers; recovery requires quiescence.
    """
    validate_session_id(session_id)
    connection = sqlite3.connect(database.DATABASE_PATH.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('BEGIN')
        meta = connection.execute(
            'SELECT cart_id, customer_uid, start_time, end_time, total_amount '
            'FROM sessions WHERE session_id = ?', (session_id,),
        ).fetchone()
        if meta is None:
            raise ProjectionError('Session does not exist')
        cart_id, customer_uid, start_time, end_time, total_amount = meta
        for value in (cart_id, customer_uid):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ProjectionError('Invalid session ownership metadata')
        start = _timestamp(start_time)
        if end_time is not None and _timestamp(end_time) < start:
            raise ProjectionError('Session completion precedes its start')
        _money(total_amount)
        rows = connection.execute(
            'SELECT b.product_id, p.name, b.quantity, b.price, b.total '
            'FROM bill_items b LEFT JOIN products p ON p.product_id = b.product_id '
            'WHERE b.session_id = ? ORDER BY b.product_id', (session_id,),
        ).fetchall()
        items, seen = [], set()
        for product_id, name, quantity, price, total in rows:
            if (type(product_id) is not int or product_id <= 0 or product_id in seen
                    or not isinstance(name, str) or not name.strip()
                    or type(quantity) is not int or quantity <= 0):
                raise ProjectionError('Invalid, duplicate or unreconstructable bill item')
            seen.add(product_id)
            _money(price)
            _money(total)
            if not math.isclose(price * quantity, total, rel_tol=1e-9, abs_tol=1e-9):
                raise ProjectionError('Bill line total is inconsistent')
            items.append(BillItemProjection(product_id, name, quantity, price, total))
        if not math.isclose(math.fsum(item.total for item in items), total_amount,
                            rel_tol=1e-9, abs_tol=1e-9):
            raise ProjectionError('Session total is inconsistent with its bill')
        return SessionProjection(session_id, cart_id, customer_uid, start_time,
                                 end_time, total_amount, tuple(items))
    finally:
        # Closing ends the read snapshot even on failure. No commit or writes.
        connection.close()
