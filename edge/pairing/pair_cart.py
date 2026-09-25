"""
edge/pairing/pair_cart.py — Core pairing logic for SmartCart.

Flow
----
1. Validate cart_id (non-empty).
2. Execute ``pair_and_assign_in_transaction`` — one ``BEGIN IMMEDIATE`` SQLite
   transaction that validates all invariants and performs the ownership
   assignment.  The transaction commits before this function returns.
3. If the transaction returns a result, synchronize to Firebase.
4. Return PairingResult.

Transaction boundary
--------------------
The SQLite ``BEGIN IMMEDIATE`` transaction (in session_store) commits before
``_default_firebase_sync`` is called.  Firebase network I/O never occurs
while the SQLite lock is held.

SQLite is authoritative.  A Firebase sync failure is logged but does NOT roll
back the committed SQLite ownership.

This module contains no HTTP logic.  The Flask layer in ``edge.api.server``
calls ``pair_customer_to_cart`` and converts results to HTTP responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from edge.pairing.session_store import (
    IntegrityConflict,
    pair_and_assign_in_transaction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result / error types
# ---------------------------------------------------------------------------

@dataclass
class PairingResult:
    """
    Result of a successful pairing operation.

    Attributes
    ----------
    session_id:
        SQLite session_id of the paired session.
    cart_id:
        Physical cart identifier.
    status:
        ``"paired"``         — new assignment committed.
        ``"already_paired"`` — idempotent; customer already owned this session.
    """
    session_id: int
    cart_id: str
    status: str


class PairingError(Exception):
    """
    Raised when a pairing request cannot be completed.

    Attributes
    ----------
    code:
        Machine-readable error code (safe for API responses).
    message:
        Human-readable description (no credentials, no stack traces).
    http_status:
        Suggested HTTP status code for the HTTP layer.
    """

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.http_status: int = http_status


# ---------------------------------------------------------------------------
# Error-code → HTTP status mapping for IntegrityConflict codes
# ---------------------------------------------------------------------------

_IC_HTTP: dict[str, int] = {
    "no_active_session_for_cart":            404,
    "multiple_active_sessions_for_cart":     409,
    "multiple_active_sessions_for_customer": 409,
    "customer_already_has_active_session":   409,
    "cart_already_assigned":                 409,
}


# ---------------------------------------------------------------------------
# Main pairing entry point
# ---------------------------------------------------------------------------

def pair_customer_to_cart(
    customer_uid: str,
    cart_id: str,
    *,
    _firebase_sync_fn=None,
) -> PairingResult:
    """
    Pair the authenticated customer to the physical cart.

    Parameters
    ----------
    customer_uid:
        Firebase Auth UID obtained from the **verified** Firebase ID token.
        This function never accepts or trusts a client-supplied UID.
    cart_id:
        Physical cart identifier from the request body.
    _firebase_sync_fn:
        Optional callable ``(session_id: int, customer_uid: str, cart_id: str) -> None``
        injected for testing.  When ``None``, the default Firebase sync path
        is used.  **For testing only.**

    Returns
    -------
    PairingResult

    Raises
    ------
    PairingError
        Any validation, conflict, or internal error.

    Transaction boundary
    --------------------
    All invariant checks and the SQLite ``UPDATE`` are performed inside one
    ``BEGIN IMMEDIATE`` transaction (see ``pair_and_assign_in_transaction``).
    That transaction commits before Firebase sync is attempted.
    """
    # ------------------------------------------------------------------
    # 1. Validate cart_id
    # ------------------------------------------------------------------
    if not cart_id or not str(cart_id).strip():
        raise PairingError(
            code="invalid_cart_id",
            message="cart_id must be a non-empty string.",
            http_status=400,
        )

    cart_id = str(cart_id).strip()

    # ------------------------------------------------------------------
    # 2. Protected transactional validation + assignment
    # ------------------------------------------------------------------
    try:
        tx_result = pair_and_assign_in_transaction(cart_id, customer_uid)
    except IntegrityConflict as exc:
        http_status = _IC_HTTP.get(exc.code, 409)
        logger.warning(
            "pair_customer_to_cart: conflict code=%r customer_uid=%r cart_id=%r: %s",
            exc.code, customer_uid, cart_id, exc,
        )
        raise PairingError(
            code=exc.code,
            message=exc.message,
            http_status=http_status,
        ) from exc
    except Exception as exc:
        logger.exception(
            "pair_customer_to_cart: unexpected error for "
            "customer_uid=%r cart_id=%r: %s",
            customer_uid, cart_id, exc,
        )
        raise PairingError(
            code="internal_error",
            message="An internal error occurred during pairing. Please retry.",
            http_status=500,
        ) from exc

    # ------------------------------------------------------------------
    # 3. Firebase synchronization (AFTER SQLite commit)
    #    Transaction is already committed; sync runs outside the lock.
    # ------------------------------------------------------------------
    if tx_result.status == "paired":
        # Only sync on new assignments; idempotent calls skip re-sync.
        sync_fn = _firebase_sync_fn if _firebase_sync_fn is not None else _default_firebase_sync
        try:
            sync_fn(tx_result.session_id, customer_uid, cart_id)
        except Exception as exc:
            # Firebase failure: log but preserve SQLite ownership.
            # SQLite is authoritative; Firebase is a downstream sync layer.
            logger.exception(
                "pair_customer_to_cart: Firebase sync failed AFTER successful "
                "SQLite commit (session_id=%d customer_uid=%r cart_id=%r). "
                "SQLite ownership is preserved. Firebase may be inconsistent "
                "until next sync. Error: %s",
                tx_result.session_id, customer_uid, cart_id, exc,
            )
            # Return success — SQLite is the truth.

    logger.info(
        "pair_customer_to_cart: %s — customer_uid=%r session_id=%d cart_id=%r",
        tx_result.status, customer_uid, tx_result.session_id, cart_id,
    )

    return PairingResult(
        session_id=tx_result.session_id,
        cart_id=cart_id,
        status=tx_result.status,
    )


# ---------------------------------------------------------------------------
# Default Firebase synchronization
# ---------------------------------------------------------------------------

def _default_firebase_sync(session_id: int, customer_uid: str, cart_id: str) -> None:
    """
    Synchronize the newly assigned session ownership to Firebase.

    Called after the SQLite ``BEGIN IMMEDIATE`` transaction has committed.
    Reads authoritative metadata from SQLite and writes to Firebase RTDB.

    Raises any Firebase exception to the caller (which logs and suppresses it).
    """
    from edge.billing.billing import get_session_meta   # noqa: PLC0415
    from edge.firebase.sync import sync_session          # noqa: PLC0415
    from edge.firebase import client as _firebase_client # noqa: PLC0415

    meta = get_session_meta(session_id)
    if meta is None:
        logger.error(
            "_default_firebase_sync: session_id=%d not found in SQLite "
            "after committed assignment; skipping Firebase sync.",
            session_id,
        )
        return

    ref = _firebase_client.get_reference("/")
    sync_session(
        session_id=meta["session_id"],
        start_time=meta["start_time"],
        end_time=meta["end_time"],
        total_amount=meta["total_amount"],
        cart_id=meta["cart_id"],
        customer_uid=meta["customer_uid"],
        _ref=ref,
    )
