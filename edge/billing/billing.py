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

from edge.database.database import get_connection
from edge.database.product_repository import get_product_by_barcode  # noqa: F401 (kept for callers)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def create_session(cart_id: str | None = None) -> int:
    """
    Create a new shopping session and return its session_id.

    Parameters
    ----------
    cart_id:
        Optional stable physical cart identifier (e.g. QR code value,
        NFC tag ID).  May be ``None`` when the cart has not yet been
        identified — it can be set later via :func:`set_session_cart_id`.

    Returns
    -------
    int
        The auto-incremented ``session_id`` of the new row.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO sessions (start_time, total_amount, cart_id)
        VALUES (datetime('now'), 0, ?)
    """, (cart_id,))

    session_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return session_id


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
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE sessions SET cart_id = ? WHERE session_id = ?
    """, (cart_id, session_id))

    connection.commit()
    connection.close()


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
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE sessions SET customer_uid = ? WHERE session_id = ?
    """, (customer_uid, session_id))

    connection.commit()
    connection.close()


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

def add_item(session_id: int, product_id: int) -> bool:
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
        (the only failure path — the SQLite write itself is not
        wrapped in a try/except).
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT price
        FROM products
        WHERE product_id = ?
    """, (product_id,))

    product = cursor.fetchone()

    if product is None:
        connection.close()
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
        (session_id, timestamp, event_type, product_id, details)
        VALUES (?, datetime('now'), ?, ?, ?)
    """, (
        session_id,
        "ADD",
        product_id,
        "Product added to bill"
    ))

    connection.commit()
    connection.close()

    return True


def remove_item(session_id: int, product_id: int) -> bool:
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
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item is None:
        connection.close()
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
        (session_id, timestamp, event_type, product_id, details)
        VALUES (?, datetime('now'), ?, ?, ?)
    """, (
        session_id,
        "REMOVE",
        product_id,
        "Product removed from bill"
    ))

    connection.commit()
    connection.close()

    return True


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def calculate_total(session_id: int) -> float:
    """
    Recalculate the running cart total from ``bill_items`` and persist
    it to ``sessions.total_amount``.

    This is the single authoritative total calculation.  Firebase sync
    must use this value — it must not maintain an independent total.

    Returns
    -------
    float
        Current total (0.0 when the bill is empty).
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT COALESCE(SUM(total), 0)
        FROM bill_items
        WHERE session_id = ?
    """, (session_id,))

    total = cursor.fetchone()[0]

    cursor.execute("""
        UPDATE sessions
        SET total_amount = ?
        WHERE session_id = ?
    """, (total, session_id))

    connection.commit()
    connection.close()

    return total


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

    Recalculates the total from ``bill_items``, then sets
    ``sessions.end_time`` and ``sessions.total_amount``.

    The Firebase sync layer should call :func:`get_session_meta` after
    this function returns to obtain the final ``end_time`` and
    ``total_amount`` for synchronization.

    Returns
    -------
    float
        Final total (same value as ``sessions.total_amount``).
    """
    total = calculate_total(session_id)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE sessions
        SET end_time = datetime('now'),
            total_amount = ?
        WHERE session_id = ?
    """, (total, session_id))

    connection.commit()
    connection.close()

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
