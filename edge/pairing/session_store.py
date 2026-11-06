"""
edge/pairing/session_store.py — Active-session lookup and atomic pairing helpers.

Responsibilities
----------------
- Look up active (not completed) sessions for a given cart_id or customer_uid.
- Enforce the single-active-session invariant:
    0 active  → return None
    1 active  → return it
    2+ active → raise IntegrityConflict (never silently select one)
- Execute validation + session creation/ownership assignment as one protected SQLite
  transaction using ``BEGIN IMMEDIATE``.

Non-responsibilities
--------------------
- Does NOT modify Firebase state.
- Does NOT use Firebase as the authoritative lookup source.
- Does NOT use completed sessions (end_time IS NOT NULL) as candidates.
- Does NOT hold the transaction open during Firebase network I/O.
- Does NOT implement HTTP or pairing business logic.

Transaction strategy
--------------------
``pair_and_assign_in_transaction`` uses ``BEGIN IMMEDIATE`` which acquires a
write (reserved) lock at the start of the transaction.  This prevents a second
concurrent writer from entering the critical section until the first commits or
rolls back.  All invariant checks (cart lookup, customer lookup, ownership
check) and the creation/assignment are inside the same connection and transaction, so
there is no check-then-write gap between separate connections.

Known limitation: ``BEGIN IMMEDIATE`` on a shared SQLite file will cause a
``sqlite3.OperationalError: database is locked`` if another writer already
holds a reserved/pending/exclusive lock (default 5-second busy timeout applies).
For the low-concurrency edge-device deployment this is acceptable.  A retry
loop or WAL-mode configuration can be added in a future hardening pass.

Testing
-------
All public functions accept optional ``_conn`` parameters so
tests can run against isolated temp databases without touching the production
file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from edge.database.database import get_connection
from edge.billing.billing import create_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class IntegrityConflict(Exception):
    """
    Raised when a data-integrity invariant is violated.

    Attributes
    ----------
    code:
        Machine-readable error string safe to return in an API response.
    message:
        Human-readable description.  No credentials or stack traces.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


# ---------------------------------------------------------------------------
# Session-meta type
# ---------------------------------------------------------------------------

SessionMeta = dict[str, Any]

_SESSION_COLS = (
    "session_id, start_time, end_time, total_amount, cart_id, customer_uid"
)


def _row_to_meta(row: tuple) -> SessionMeta:
    return {
        "session_id":   row[0],
        "start_time":   row[1],
        "end_time":     row[2],
        "total_amount": row[3],
        "cart_id":      row[4],
        "customer_uid": row[5],
    }


# ---------------------------------------------------------------------------
# Internal helpers (operate on a caller-supplied connection)
# ---------------------------------------------------------------------------

def _fetch_active_for_cart(cur: sqlite3.Cursor, cart_id: str) -> list[tuple]:
    cur.execute(
        f"SELECT {_SESSION_COLS} FROM sessions "
        "WHERE cart_id = ? AND end_time IS NULL ORDER BY session_id",
        (cart_id,),
    )
    return cur.fetchall()


def _fetch_active_for_customer(cur: sqlite3.Cursor, customer_uid: str) -> list[tuple]:
    cur.execute(
        f"SELECT {_SESSION_COLS} FROM sessions "
        "WHERE customer_uid = ? AND end_time IS NULL ORDER BY session_id",
        (customer_uid,),
    )
    return cur.fetchall()


def _single_or_conflict(
    rows: list[tuple],
    conflict_code: str,
    conflict_msg: str,
) -> SessionMeta | None:
    """Return None / single meta / raise IntegrityConflict."""
    if len(rows) == 0:
        return None
    if len(rows) == 1:
        return _row_to_meta(rows[0])
    ids = [r[0] for r in rows]
    logger.error("IntegrityConflict %s: session_ids=%r", conflict_code, ids)
    raise IntegrityConflict(code=conflict_code, message=conflict_msg)


# ---------------------------------------------------------------------------
# Read-only public helpers (for tests and non-transactional callers)
# ---------------------------------------------------------------------------

def get_active_session_for_cart(
    cart_id: str,
    *,
    _conn=None,
) -> SessionMeta | None:
    """
    Return the single active session for *cart_id*, or None.

    Raises ``IntegrityConflict(code="multiple_active_sessions_for_cart")``
    when more than one active session exists.
    """
    own_conn = _conn is None
    conn = get_connection() if own_conn else _conn
    try:
        rows = _fetch_active_for_cart(conn.cursor(), cart_id)
    finally:
        if own_conn:
            conn.close()
    return _single_or_conflict(
        rows,
        "multiple_active_sessions_for_cart",
        f"Multiple active sessions found for cart_id={cart_id!r}. "
        "Backend reconciliation required.",
    )


def get_active_session_for_customer(
    customer_uid: str,
    *,
    _conn=None,
) -> SessionMeta | None:
    """
    Return the single active session for *customer_uid*, or None.

    Raises ``IntegrityConflict(code="multiple_active_sessions_for_customer")``
    when more than one active session exists.
    """
    own_conn = _conn is None
    conn = get_connection() if own_conn else _conn
    try:
        rows = _fetch_active_for_customer(conn.cursor(), customer_uid)
    finally:
        if own_conn:
            conn.close()
    return _single_or_conflict(
        rows,
        "multiple_active_sessions_for_customer",
        f"Multiple active sessions found for customer_uid={customer_uid!r}. "
        "Backend reconciliation required.",
    )


# ---------------------------------------------------------------------------
# Transactional pairing result
# ---------------------------------------------------------------------------

@dataclass
class TransactionResult:
    """
    Result returned by ``pair_and_assign_in_transaction``.

    Attributes
    ----------
    session_id:
        The SQLite session that was paired or confirmed.
    cart_id:
        Physical cart identifier.
    customer_uid:
        The Firebase UID that was assigned.
    status:
        ``"paired"``        — new assignment committed.
        ``"already_paired"``— customer already owned this session; no write.
    """
    session_id: int
    cart_id: str
    customer_uid: str
    status: str


# ---------------------------------------------------------------------------
# Protected transactional pair-and-assign
# ---------------------------------------------------------------------------

def pair_and_assign_in_transaction(
    cart_id: str,
    customer_uid: str,
    *,
    _conn=None,
) -> TransactionResult:
    """
    Validate all pairing invariants and create or assign a session inside one
    ``BEGIN IMMEDIATE`` SQLite transaction.

    This is the authoritative mutation entry point.  All invariant checks
    (cart lookup, session-state check, session ownership check, customer
    uniqueness check) and creation/assignment occur inside the same connection and
    transaction, eliminating the check-then-write gap that existed when these
    were separate operations.

    Invariants checked inside the transaction
    -----------------------------------------
    1. *cart_id* must be in SMARTCART_ALLOWED_CARTS (default: CART-001).
       Comma-separated IDs are trimmed, empty entries ignored, case preserved.
       An explicitly empty configuration allows no carts.
    2. *cart_id* has at most one active (``end_time IS NULL``) session.
       Zero  → create a fresh owned session after customer uniqueness checks.
       2+    → ``IntegrityConflict(code="multiple_active_sessions_for_cart")``
    3. Customer has at most one active session.
       2+    → ``IntegrityConflict(code="multiple_active_sessions_for_customer")``
    4. If customer owns a different active session:
              → ``IntegrityConflict(code="customer_already_has_active_session")``
    5. If customer already owns THIS session → idempotent ``already_paired``.
    6. If session is already assigned to a different UID:
              → ``IntegrityConflict(code="cart_already_assigned")``
    7. If session is unassigned → ``UPDATE`` assigns *customer_uid* and commits.

    Transaction boundary
    --------------------
    The SQLite transaction is committed **before** this function returns.
    Firebase synchronization is the caller's responsibility and must occur
    **after** this function returns successfully.  The transaction is never
    held open during Firebase network I/O.

    Parameters
    ----------
    cart_id:
        Physical cart identifier.
    customer_uid:
        Verified Firebase Auth UID (from a verified ID token).
    _conn:
        Optional idle SQLite connection. This function owns the transaction
        (including commit/rollback), but does not close an injected connection.
        An already-active transaction is rejected without changing it.  When ``None`` (default), this function
        opens a connection, executes ``BEGIN IMMEDIATE``, commits or rolls
        back, and closes the connection.

    Returns
    -------
    TransactionResult

    Raises
    ------
    IntegrityConflict
        Any invariant violation.  See codes above.
    sqlite3.OperationalError
        If the database is locked (another writer holds ``BEGIN IMMEDIATE``).
        Callers may retry after a short delay.
    """
    own_conn = _conn is None

    conn = get_connection() if own_conn else _conn
    started = False
    try:
        if conn.in_transaction:
            raise ValueError("Pairing requires an idle SQLite connection")
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        started = True

        # ------------------------------------------------------------------
        # Eligibility and both ownership lookups share the writer transaction.
        # ------------------------------------------------------------------
        allowed_carts = {
            value.strip()
            for value in os.environ.get("SMARTCART_ALLOWED_CARTS", "CART-001").split(",")
            if value.strip()
        }
        if cart_id not in allowed_carts:
            raise IntegrityConflict(
                code="cart_not_allowed",
                message="This cart is not enabled for pairing on this backend.",
            )
        customer_rows = _fetch_active_for_customer(cur, customer_uid)
        cart_rows = _fetch_active_for_cart(cur, cart_id)

        if len(cart_rows) > 1:
            ids = [r[0] for r in cart_rows]
            logger.error(
                "pair_and_assign_in_transaction: multiple active sessions "
                "for cart_id=%r: %r", cart_id, ids,
            )
            raise IntegrityConflict(
                code="multiple_active_sessions_for_cart",
                message=(
                    f"Multiple active sessions found for cart_id={cart_id!r}. "
                    "Backend reconciliation required."
                ),
            )

        cart_session = _row_to_meta(cart_rows[0]) if cart_rows else None
        session_id = cart_session["session_id"] if cart_session else None
        session_owner = cart_session["customer_uid"] if cart_session else None
        if cart_session is not None:
            from edge.billing.checkout import checkout_history, CheckoutError
            marker = cur.execute('SELECT checkout_started_at FROM sessions WHERE session_id=?',
                                 (session_id,)).fetchone()[0]
            try:
                checkout_history(conn, session_id, cart_session['end_time'], marker)
            except CheckoutError:
                raise IntegrityConflict('checkout_integrity', 'Checkout lifecycle requires backend inspection.') from None

        # ------------------------------------------------------------------
        # Invariant 3 & 4: customer uniqueness
        # ------------------------------------------------------------------
        if len(customer_rows) > 1:
            ids = [r[0] for r in customer_rows]
            logger.error(
                "pair_and_assign_in_transaction: multiple active sessions "
                "for customer_uid=%r: %r", customer_uid, ids,
            )
            raise IntegrityConflict(
                code="multiple_active_sessions_for_customer",
                message=(
                    f"Multiple active sessions found for customer_uid={customer_uid!r}. "
                    "Backend reconciliation required."
                ),
            )

        if len(customer_rows) == 1:
            existing_sid = customer_rows[0][0]
            if existing_sid != session_id:
                raise IntegrityConflict(
                    code="customer_already_has_active_session",
                    message=(
                        "This customer already has an active session. "
                        "Complete or release the existing session before "
                        "pairing to a new cart."
                    ),
                )
            # Invariant 5: customer already owns THIS session — idempotent.
            conn.commit()
            logger.info(
                "pair_and_assign_in_transaction: idempotent — "
                "customer_uid=%r already owns session_id=%d cart_id=%r",
                customer_uid, session_id, cart_id,
            )
            return TransactionResult(
                session_id=session_id,
                cart_id=cart_id,
                customer_uid=customer_uid,
                status="already_paired",
            )

        # ------------------------------------------------------------------
        # Invariant 6: session ownership
        # ------------------------------------------------------------------
        if session_owner is not None and session_owner != customer_uid:
            raise IntegrityConflict(
                code="cart_already_assigned",
                message="This cart is already assigned to another customer.",
            )

        if session_owner == customer_uid:
            # Idempotent: session already assigned to same UID but customer
            # lookup returned 0 rows — defensive path (shouldn't happen
            # with consistent data).
            conn.commit()
            return TransactionResult(
                session_id=session_id,
                cart_id=cart_id,
                customer_uid=customer_uid,
                status="already_paired",
            )

        # ------------------------------------------------------------------
        # Invariant 7: create or assign, without leaving this transaction.
        # ------------------------------------------------------------------
        if cart_session is None:
            session_id = create_session(cart_id, customer_uid=customer_uid, _conn=conn)
        else:
            frozen = cur.execute(
                'SELECT checkout_started_at FROM sessions WHERE session_id = ?',
                (session_id,),
            ).fetchone()[0]
            if frozen is not None or cur.execute('SELECT 1 FROM checkouts WHERE session_id=?', (session_id,)).fetchone():
                raise IntegrityConflict('checkout_frozen', 'Frozen session cannot be reassigned.')
            cur.execute(
                "UPDATE sessions SET customer_uid = ? WHERE session_id = ?",
                (customer_uid, session_id),
            )

        conn.commit()

        logger.info(
            "pair_and_assign_in_transaction: committed — "
            "customer_uid=%r session_id=%d cart_id=%r",
            customer_uid, session_id, cart_id,
        )
        return TransactionResult(
            session_id=session_id,
            cart_id=cart_id,
            customer_uid=customer_uid,
            status="paired",
        )

    except Exception:
        if started:
            conn.rollback()
        raise

    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Legacy standalone helper (kept for existing tests that call it directly)
# ---------------------------------------------------------------------------

def assign_customer_atomic(
    session_id: int,
    customer_uid: str,
    *,
    _conn=None,
) -> None:
    """
    Assign *customer_uid* to *session_id* with a transactional re-read guard.

    .. deprecated::
        New code should use :func:`pair_and_assign_in_transaction` which
        performs all invariant checks inside one ``BEGIN IMMEDIATE`` transaction.
        This function is retained for existing test coverage.

    Raises
    ------
    IntegrityConflict
        ``code="cart_already_assigned"`` if a different UID is already set.
    ValueError
        If *session_id* does not exist.
    """
    own_conn = _conn is None
    conn = get_connection() if own_conn else _conn
    if own_conn:
        conn.isolation_level = None

    try:
        cur = conn.cursor()
        if own_conn:
            cur.execute("BEGIN IMMEDIATE")

        cur.execute(
            "SELECT customer_uid, checkout_started_at FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"session_id={session_id} does not exist")

        current_uid = row[0]
        if current_uid is not None and current_uid != customer_uid:
            raise IntegrityConflict(
                code="cart_already_assigned",
                message=(
                    f"Session {session_id} was concurrently assigned to "
                    "another customer before this request could complete."
                ),
            )

        if current_uid == customer_uid:
            if own_conn:
                cur.execute("COMMIT")
            return

        if row[1] is not None or cur.execute('SELECT 1 FROM checkouts WHERE session_id=?', (session_id,)).fetchone():
            raise IntegrityConflict('checkout_frozen', 'Frozen session cannot be reassigned.')
        cur.execute(
            "UPDATE sessions SET customer_uid = ? WHERE session_id = ?",
            (customer_uid, session_id),
        )
        if own_conn:
            cur.execute("COMMIT")

    except Exception:
        if own_conn:
            try:
                cur.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        raise

    finally:
        if own_conn:
            conn.close()
