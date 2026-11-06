"""
edge/billing/billing.py — SmartCart billing operations against SQLite.

Responsibilities:
    - Create, update, and complete shopping sessions in SQLite.
    - Maintain bill_items and session_log tables.
    - Expose session metadata (start_time, end_time, total_amount,
      cart_id, customer_uid) needed by the Firebase sync layer.

Non-responsibilities:
    - Does NOT call Firebase sync directly.  That belongs to CartSession,
      which calls sync *after* these functions return True.
    - Does NOT contain detection, YOLO, or weight-sensor logic.
    - Does NOT implement CartState.

SQLite-authoritative rule
-------------------------
These functions are the single source of truth for all billing state.
Firebase synchronization happens *after* every successful call to the
functions in this module.  A Firebase failure never causes a SQLite
rollback (see CartSession for the failure boundary).
"""

from contextlib import contextmanager

from edge.database.database import get_connection
from edge.database.product_repository import get_product_by_barcode  # noqa: F401 (kept for callers)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def create_session(
    cart_id: str | None = None, *, customer_uid: str | None = None, _conn=None,
) -> int:
    """
    Create a new shopping session and return its session_id.

    Parameters
    ----------
    cart_id:
        Optional stable physical cart identifier (e.g. QR code value,
        NFC tag ID).  May be ``None`` when the cart has not yet been
        identified — it can be set later via :func:`set_session_cart_id`.
    customer_uid:
        Optional verified owner supplied by the protected pairing path.
    _conn:
        Optional connection with an active caller-owned transaction. This
        function neither commits, rolls back nor closes that transaction.
        Without it, creation commits and closes its own connection as before.

    Returns
    -------
    int
        The auto-incremented ``session_id`` of the new row.
    """
    own_conn = _conn is None
    connection = get_connection() if own_conn else _conn
    try:
        if not own_conn and not connection.in_transaction:
            raise ValueError("Session creation requires an active caller transaction")
        cursor = connection.execute("""
            INSERT INTO sessions (start_time, total_amount, cart_id, customer_uid)
            VALUES (datetime('now'), 0, ?, ?)
        """, (cart_id, customer_uid))
        session_id = cursor.lastrowid
        if own_conn:
            connection.commit()
        return session_id
    except BaseException:
        if own_conn:
            connection.rollback()
        raise
    finally:
        if own_conn:
            connection.close()


def set_session_cart_id(session_id: int, cart_id: str) -> None:
    """
    Assign a stable physical cart identifier to an existing session.

    This is the write-side of the cart ownership model.  The cart_id
    is set once when the cart is identified (e.g. QR scan, NFC tap)
    and does not change during the session.

    Parameters
    ----------
    session_id:
        Target session.
    cart_id:
        Stable physical cart identifier string.
    """
    with _active_mutation(session_id, shopping=True) as connection:
        _protect_checkout_owner(connection, session_id, 'cart_id', cart_id)
        connection.execute('UPDATE sessions SET cart_id = ? WHERE session_id = ?',
                           (cart_id, session_id))


def assign_customer_to_session(session_id: int, customer_uid: str) -> None:
    """
    Assign an authenticated customer's Firebase UID to an active session.

    This is the write-side of the customer ownership model.  The
    customer_uid is the Firebase Auth UID obtained from the customer's
    authenticated session (e.g. after QR scan on the Flutter app).

    Parameters
    ----------
    session_id:
        Target session.
    customer_uid:
        Firebase Auth UID of the authenticated customer.
    """
    with _active_mutation(session_id, shopping=True) as connection:
        _protect_checkout_owner(connection, session_id, 'customer_uid', customer_uid)
        connection.execute('UPDATE sessions SET customer_uid = ? WHERE session_id = ?',
                           (customer_uid, session_id))


def get_session_meta(session_id: int) -> dict | None:
    """
    Return session metadata needed by the Firebase sync layer.

    Returns a dict with keys:
        ``session_id``, ``start_time``, ``end_time``,
        ``total_amount``, ``cart_id``, ``customer_uid``

    or ``None`` if the session does not exist.

    This is the read-side helper used by CartSession after each
    SQLite mutation to build the Firebase sync payload.  Keeping it
    here (rather than in sync.py) preserves the principle that all
    SQLite reads belong to the billing layer.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            session_id,
            start_time,
            end_time,
            total_amount,
            cart_id,
            customer_uid
        FROM sessions
        WHERE session_id = ?
    """, (session_id,))

    row = cursor.fetchone()
    connection.close()

    if row is None:
        return None

    return {
        "session_id":   row[0],
        "start_time":   row[1],
        "end_time":     row[2],
        "total_amount": row[3],
        "cart_id":      row[4],
        "customer_uid": row[5],
    }


# ---------------------------------------------------------------------------
# Bill-item operations
# ---------------------------------------------------------------------------

class InactiveSessionError(ValueError):
    """The authoritative session is missing or completed."""


class CheckoutFrozenError(InactiveSessionError):
    """Checkout has frozen this session; shopping cannot resume."""


class StaleRuntimeError(InactiveSessionError):
    """A checkout permanently invalidated this runtime's hydrated generation."""


class DuplicateEventError(ValueError):
    """An event ID has already produced a billing mutation."""


def add_runtime_item(session_id, product_id, *, expected_generation, event_id=None, details=None):
    """Production runtime entry point: a captured generation is mandatory."""
    if type(expected_generation) is not int or expected_generation < 0:
        raise StaleRuntimeError('Runtime generation is required')
    return add_item(session_id, product_id, expected_generation=expected_generation,
                    event_id=event_id, details=details)


def remove_runtime_item(session_id, product_id, *, expected_generation, event_id=None, details=None):
    """Production runtime entry point: never fall back to an unguarded call."""
    if type(expected_generation) is not int or expected_generation < 0:
        raise StaleRuntimeError('Runtime generation is required')
    return remove_item(session_id, product_id, expected_generation=expected_generation,
                       event_id=event_id, details=details)


def _protect_checkout_owner(connection, session_id, column, value):
    if connection.execute('SELECT 1 FROM checkouts WHERE session_id=?', (session_id,)).fetchone():
        current = connection.execute(f'SELECT {column} FROM sessions WHERE session_id=?',
                                     (session_id,)).fetchone()[0]
        if current != value:
            raise InactiveSessionError('Checkout history prevents ownership reassignment')


@contextmanager
def _active_mutation(session_id, *, shopping=False, expected_generation=None):
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT end_time, checkout_started_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None or row[0] is not None:
            raise InactiveSessionError("Session is missing or completed")
        from edge.billing.checkout import checkout_history
        history = checkout_history(connection, session_id, row[0], row[1])
        if shopping and row[1] is not None:
            raise CheckoutFrozenError("Session checkout is frozen")
        generation = history[-1]['checkout_id'] if history else 0
        if expected_generation is not None and (type(expected_generation) is not int
                                                or expected_generation != generation):
            raise StaleRuntimeError('Stale runtime generation; reconcile and start a fresh runtime')
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _update_total(cursor, session_id):
    cursor.execute(
        "SELECT COALESCE(SUM(total), 0) FROM bill_items WHERE session_id = ?",
        (session_id,),
    )
    total = cursor.fetchone()[0]
    cursor.execute("UPDATE sessions SET total_amount = ? WHERE session_id = ?",
                   (total, session_id))
    return total


def get_cart_state_rows(session_id):
    """Read resume quantities/classes in one snapshot; reject corrupt joins."""
    return hydrate_cart(session_id)[0]


def hydrate_cart(session_id):
    """Hydrate bill, shopping state and generation under one transaction."""
    with _active_mutation(session_id, shopping=True) as connection:
        rows = connection.execute(
            "SELECT b.product_id, p.yolo_class, b.quantity FROM bill_items b "
            "LEFT JOIN products p ON p.product_id = b.product_id "
            "WHERE b.session_id = ?", (session_id,)
        ).fetchall()
        generation = connection.execute('SELECT COALESCE(MAX(checkout_id),0) FROM checkouts WHERE session_id=?',
                                        (session_id,)).fetchone()[0]
        return rows, generation


def add_item(session_id: int, product_id: int, *, expected_generation=None,
             event_id=None, details=None) -> bool:
    """
    Add one unit of *product_id* to the bill for *session_id*.

    Looks up the current price from ``products`` and UPSERTs the
    ``bill_items`` row (creating it at quantity=1 on first add, or
    incrementing quantity on subsequent adds).  Appends an ADD entry
    to ``session_log``.

    Returns
    -------
    bool
        ``True`` on success.
        ``False`` when *product_id* does not exist in ``products``
        Missing/completed sessions raise InactiveSessionError. SQLite errors
        roll back the transaction and propagate.
    """
    with _active_mutation(session_id, shopping=True, expected_generation=expected_generation) as connection:
        cursor = connection.cursor()
        if event_id is not None and cursor.execute(
                'SELECT 1 FROM session_log WHERE event_id = ?', (event_id,)).fetchone():
            raise DuplicateEventError(f'Event {event_id!r} was already processed')

        cursor.execute("""
            SELECT price
            FROM products
            WHERE product_id = ?
        """, (product_id,))

        product = cursor.fetchone()

        if product is None:
            return False

        price = product[0]

        cursor.execute("""
            SELECT bill_item_id, quantity
            FROM bill_items
            WHERE session_id = ? AND product_id = ?
        """, (session_id, product_id))

        item = cursor.fetchone()

        if item:
            bill_item_id, quantity = item

            new_quantity = quantity + 1
            new_total = price * new_quantity

            cursor.execute("""
                UPDATE bill_items
                SET quantity = ?,
                    price = ?,
                    total = ?
                WHERE bill_item_id = ?
            """, (
                new_quantity,
                price,
                new_total,
                bill_item_id
            ))

        else:
            cursor.execute("""
                INSERT INTO bill_items
                (session_id, product_id, quantity, price, total)
                VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                product_id,
                1,
                price,
                price
            ))

        cursor.execute("""
            INSERT INTO session_log
            (session_id, timestamp, event_type, product_id, details, event_id)
            VALUES (?, datetime('now'), ?, ?, ?, ?)
        """, (
            session_id,
            "ADD",
            product_id,
            details if details is not None else "Product added to bill",
            event_id,
        ))


        _update_total(cursor, session_id)
        return True


def remove_item(session_id: int, product_id: int, *, expected_generation=None,
                event_id=None, details=None) -> bool:
    """
    Remove one unit of *product_id* from the bill for *session_id*.

    Decrements the ``bill_items`` quantity by 1.  When quantity
    reaches zero the row is deleted (mirrors CartState behaviour).
    Appends a REMOVE entry to ``session_log``.

    Returns
    -------
    bool
        ``True`` on success.
        ``False`` when no ``bill_items`` row exists for this
        ``(session_id, product_id)`` pair.
    """
    with _active_mutation(session_id, shopping=True, expected_generation=expected_generation) as connection:
        cursor = connection.cursor()
        if event_id is not None and cursor.execute(
                'SELECT 1 FROM session_log WHERE event_id = ?', (event_id,)).fetchone():
            raise DuplicateEventError(f'Event {event_id!r} was already processed')

        cursor.execute("""
            SELECT bill_item_id, quantity
            FROM bill_items
            WHERE session_id = ? AND product_id = ?
        """, (session_id, product_id))

        item = cursor.fetchone()

        if item is None:
            return False

        bill_item_id, quantity = item

        if quantity > 1:
            cursor.execute("""
                SELECT price
                FROM bill_items
                WHERE bill_item_id = ?
            """, (bill_item_id,))

            price = cursor.fetchone()[0]
            new_quantity = quantity - 1
            new_total = price * new_quantity

            cursor.execute("""
                UPDATE bill_items
                SET quantity = ?,
                    total = ?
                WHERE bill_item_id = ?
            """, (
                new_quantity,
                new_total,
                bill_item_id
            ))

        else:
            cursor.execute("""
                DELETE FROM bill_items
                WHERE bill_item_id = ?
            """, (bill_item_id,))

        cursor.execute("""
            INSERT INTO session_log
            (session_id, timestamp, event_type, product_id, details, event_id)
            VALUES (?, datetime('now'), ?, ?, ?, ?)
        """, (
            session_id,
            "REMOVE",
            product_id,
            details if details is not None else "Product removed from bill",
            event_id,
        ))


        _update_total(cursor, session_id)
        return True


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def calculate_total(session_id: int) -> float:
    """Read the authoritative committed total without mutating billing state.

    ADD/REMOVE and completion maintain this value atomically with bill items.
    Retains the float return and legacy zero result for a missing session;
    completed sessions may be read but are never rewritten by this helper.
    """
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT total_amount FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return float(row[0]) if row is not None else 0.0
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Bill queries
# ---------------------------------------------------------------------------

def get_bill(session_id: int) -> list:
    """
    Return the current bill as a list of tuples.

    Each tuple: ``(product_id, name, quantity, price, total)``.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            products.product_id,
            products.name,
            bill_items.quantity,
            bill_items.price,
            bill_items.total
        FROM bill_items
        JOIN products
            ON bill_items.product_id = products.product_id
        WHERE bill_items.session_id = ?
        ORDER BY products.product_id
    """, (session_id,))

    bill = cursor.fetchall()

    connection.close()

    return bill


def get_bill_item(session_id: int, product_id: int) -> tuple | None:
    """
    Return a single bill-item row for Firebase sync, or ``None``.

    Returns a tuple: ``(product_id, name, quantity, price, total)``
    matching the structure returned by :func:`get_bill`.

    Used by CartSession after a successful :func:`add_item` or
    :func:`remove_item` to retrieve the updated row for Firebase
    synchronization without re-fetching the entire bill.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            products.product_id,
            products.name,
            bill_items.quantity,
            bill_items.price,
            bill_items.total
        FROM bill_items
        JOIN products
            ON bill_items.product_id = products.product_id
        WHERE bill_items.session_id = ?
          AND bill_items.product_id = ?
    """, (session_id, product_id))

    row = cursor.fetchone()
    connection.close()

    return row


def get_latest_log_entry(session_id: int) -> tuple | None:
    """
    Return the most recent session_log entry for *session_id*.

    Returns a tuple:
        ``(log_id, session_id, timestamp, event_type, product_id, details)``

    or ``None`` if no entries exist yet.

    Used by CartSession to retrieve the timestamp of the event that was
    just committed so it can be forwarded to Firebase event_log.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            log_id,
            session_id,
            timestamp,
            event_type,
            product_id,
            details
        FROM session_log
        WHERE session_id = ?
        ORDER BY log_id DESC
        LIMIT 1
    """, (session_id,))

    row = cursor.fetchone()
    connection.close()

    return row


# ---------------------------------------------------------------------------
# Session completion
# ---------------------------------------------------------------------------

def complete_session(session_id: int) -> float:
    """
    Mark the session as complete and return the final total.

    Validates and preserves a current checkout's immutable amount, otherwise
    recalculates the total from ``bill_items``. Sets ``sessions.end_time`` and
    transitions the current checkout to COMPLETED atomically. This trusted
    operation does not verify payment and rejects any payment reservation.

    The Firebase sync layer should call :func:`get_session_meta` after
    this function returns to obtain the final ``end_time`` and
    ``total_amount`` for synchronization.

    Returns
    -------
    float
        Final total (same value as ``sessions.total_amount``).
    """
    with _active_mutation(session_id) as connection:
        cursor = connection.cursor()
        if connection.execute('''SELECT 1 FROM checkouts c WHERE c.session_id=? AND
                (c.payment_started_at IS NOT NULL OR EXISTS(
                    SELECT 1 FROM payment_attempts p WHERE p.checkout_id=c.checkout_id))''',
                              (session_id,)).fetchone():
            from edge.payment.money import PaymentError
            raise PaymentError('payment_started', 'Reserved payment requires verified paid completion.')
        frozen = connection.execute("SELECT amount FROM checkouts WHERE session_id=? AND state='FROZEN'",
                                    (session_id,)).fetchone()
        if frozen is not None:
            from edge.billing.checkout import _validate_bill
            _validate_bill(connection, session_id, frozen[0])
            # Preserve the already validated quote exactly, including existing
            # floating-point tolerance. Completion must not reprice the snapshot.
            total = frozen[0]
        else:
            total = _update_total(cursor, session_id)
        # Trusted completion is not payment verification. Keep the exact
        # current checkout historical, in the same transaction as end_time.
        connection.execute("UPDATE checkouts SET state='COMPLETED' WHERE session_id=? AND state='FROZEN'",
                           (session_id,))
        cursor.execute(
            "UPDATE sessions SET end_time = datetime('now') WHERE session_id = ?",
            (session_id,),
        )
        return total


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------

def get_session_log(session_id: int) -> list:
    """
    Return the ordered ADD/REMOVE event log for *session_id*.

    Each row: ``(log_id, session_id, timestamp, event_type, product_id, details)``.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            log_id,
            session_id,
            timestamp,
            event_type,
            product_id,
            details
        FROM session_log
        WHERE session_id = ?
        ORDER BY log_id
    """, (session_id,))

    logs = cursor.fetchall()

    connection.close()

    return logs
