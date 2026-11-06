"""
edge/firebase/sync.py — Firebase synchronization layer for SmartCart.

Responsibilities:
    - Transform SmartCart SQLite data into the approved Firebase schema.
    - Write that data through the Firebase client reference API.
    - Surface Firebase write failures to the caller without touching SQLite.

Non-responsibilities:
    - Does NOT initialize the Firebase Admin SDK (that belongs to client.py).
    - Does NOT modify SQLite state.
    - Does NOT implement offline queues, retry logic, or backoff.
    - Does NOT contain billing, detection, or session-management logic.
    - Does NOT read from Firebase.
    - Does NOT know about CartState, ProductResolver, or MOG2.

Architectural rule
------------------
SQLite is the authoritative source of truth.  Every sync function is called
*after* the corresponding SQLite transaction has committed successfully.
A Firebase failure is surfaced to the caller as an exception; it never
causes a SQLite rollback or modifies local billing state.

Failure boundary
----------------
Firebase failures propagate as exceptions to the caller (CartSession).
CartSession logs them but does NOT roll back the SQLite transaction —
SQLite success and Firebase success are independent.  This is the intended
behaviour: the cart is billed correctly even if a Firebase write fails.
An offline queue / replay system can be layered on top later without
changing this module.

Approved Firebase schema (subset handled here)::

    /
    ├── products/{product_id}/
    │   ├── name
    │   ├── barcode
    │   ├── price
    │   ├── expected_weight
    │   └── yolo_class
    │
    └── sessions/{session_id}/
        ├── cart_id
        ├── customer_uid
        ├── start_time
        ├── end_time
        ├── total_amount
        │
        ├── bill_items/{product_id}/
        │   ├── product_name
        │   ├── quantity
        │   ├── price
        │   └── total
        │
        └── event_log/{firebase_push_key}/
            ├── timestamp
            ├── event_type
            └── product_id

Dependency injection
--------------------
Every public function accepts an optional ``_ref`` parameter.  When
supplied, it is used directly instead of calling
``edge.firebase.client.get_reference()``.  This allows the test suite to
inject a fake reference tree without touching a real Firebase project.
The same injection pattern is established by ``YOLODetector._model`` and
``client.initialize(_fake_ref=...)``.

Testing
-------
Pass a ``_FakeReference`` (defined in ``test_firebase_sync.py``) via the
``_ref`` keyword argument to exercise every sync function without any
real Firebase connection, credentials, or internet access.
"""

from __future__ import annotations

from edge.firebase import client as _firebase_client


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _root_ref(_ref=None):
    """
    Return the root Firebase reference.

    If ``_ref`` is supplied (testing injection), return it directly.
    Otherwise, delegate to the initialized Firebase client.
    """
    if _ref is not None:
        return _ref
    return _firebase_client.get_reference("/")


# ---------------------------------------------------------------------------
# Product sync
# ---------------------------------------------------------------------------

def sync_product(
    product_id: int,
    name: str,
    barcode: str | None,
    price: float,
    expected_weight: float,
    yolo_class: str | None,
    *,
    _ref=None,
) -> None:
    """
    Write one product to Firebase at ``products/{product_id}/``.

    Parameters
    ----------
    product_id:
        The SQLite ``products.product_id`` value.  Used as the Firebase key.
    name:
        Human-readable product name (e.g. ``"Maggi Small"``).
    barcode:
        EAN/UPC barcode string, or ``None`` if not set.
    price:
        Unit price in local currency.
    expected_weight:
        Expected weight in grams used by the weight-sensor resolver.
    yolo_class:
        The YOLO class label (e.g. ``"Maggi_10"``), or ``None`` if not set.
    _ref:
        Injected root reference for testing.  When ``None``, the live
        Firebase client reference is used.

    Raises
    ------
    Exception
        Any exception raised by the Firebase SDK is propagated directly to
        the caller.  SQLite state is never modified by this function.

    Notes
    -----
    The Firebase key is the string representation of ``product_id``.
    Firebase Realtime Database keys must be strings; the SDK coerces the
    integer path segment automatically, but using the integer directly in
    the path string is explicit and unambiguous.
    """
    data = {
        "name": name,
        "barcode": barcode,
        "price": price,
        "expected_weight": expected_weight,
        "yolo_class": yolo_class,
    }

    _root_ref(_ref).child(f"products/{product_id}").set(data)


# ---------------------------------------------------------------------------
# Session sync
# ---------------------------------------------------------------------------

def sync_session(
    session_id: int,
    start_time: str,
    end_time: str | None,
    total_amount: float,
    *,
    cart_id: str | None = None,
    customer_uid: str | None = None,
    _ref=None,
) -> None:
    """
    Write session metadata to Firebase at ``sessions/{session_id}/``.

    Uses ``update()`` (not ``set()``) so that sibling nodes
    (``bill_items/``, ``event_log/``) that already exist under the
    session path are not deleted.

    Parameters
    ----------
    session_id:
        The SQLite ``sessions.session_id`` value.  Used as the Firebase key.
    start_time:
        SQLite ``datetime('now')`` string written by ``billing.create_session()``.
    end_time:
        SQLite ``datetime('now')`` string written by ``billing.complete_session()``,
        or ``None`` when the session is still active.  Active vs completed
        state is derivable in Flutter from ``end_time == null``.
    total_amount:
        Running cart total.  Updated on every ADD/REMOVE by
        ``billing.calculate_total()`` and finalised by
        ``billing.complete_session()``.
    cart_id:
        Stable physical cart identifier, or ``None`` when not yet assigned.
        Represents the physical cart this session is associated with.
    customer_uid:
        Firebase Auth UID of the assigned customer, or ``None`` when no
        customer has been assigned yet.  Firebase security rules use this
        value to restrict customer read access to their own session.
    _ref:
        Injected root reference for testing.

    Raises
    ------
    Exception
        Any Firebase SDK exception is propagated to the caller.

    Notes
    -----
    ``status`` is intentionally omitted — it is derivable from
    ``end_time == null`` in the Flutter client.
    """
    data = {
        "start_time":   start_time,
        "end_time":     end_time,
        "total_amount": total_amount,
        "cart_id":      cart_id,
        "customer_uid": customer_uid,
    }

    _root_ref(_ref).child(f"sessions/{session_id}").update(data)


# ---------------------------------------------------------------------------
# Bill item sync
# ---------------------------------------------------------------------------

def sync_session_projection(snapshot, *, _ref=None) -> None:
    """Publish a validated SessionProjection in one controlled recovery write.

    A top-level bill_items value replaces that complete subtree; None removes
    it for an empty bill. update() preserves event_log and unrelated children.
    This does not order concurrent writers or provide exactly-once delivery.
    """
    bill_items = {
        str(item.product_id): {
            'product_name': item.product_name,
            'quantity': item.quantity,
            'price': item.price,
            'total': item.total,
        }
        for item in snapshot.bill_items
    }
    data = {
        'cart_id': snapshot.cart_id,
        'customer_uid': snapshot.customer_uid,
        'start_time': snapshot.start_time,
        'end_time': snapshot.end_time,
        'total_amount': snapshot.total_amount,
        'bill_items': bill_items or None,
    }
    _root_ref(_ref).child(f'sessions/{snapshot.session_id}').update(data)


def sync_bill_item(
    session_id: int,
    product_id: int,
    product_name: str,
    quantity: int,
    price: float,
    total: float,
    *,
    _ref=None,
) -> None:
    """
    Write one bill item to Firebase at
    ``sessions/{session_id}/bill_items/{product_id}/``.

    The Firebase key is ``product_id``, which matches the SQLite billing
    behaviour: ``billing.add_item()`` and ``billing.remove_item()`` both
    UPSERT by ``(session_id, product_id)`` — there is at most one bill-item
    row per product per session.

    Parameters
    ----------
    session_id:
        Parent session identifier.
    product_id:
        Product identifier; used as the Firebase key under ``bill_items/``.
    product_name:
        Denormalized from ``products.name`` so the Flutter customer app can
        render the bill without querying ``products/`` separately.
    quantity:
        Current unit count for this product in the session.
    price:
        Unit price at time of billing.
    total:
        ``price * quantity``.
    _ref:
        Injected root reference for testing.

    Raises
    ------
    Exception
        Any Firebase SDK exception is propagated to the caller.
    """
    data = {
        "product_name": product_name,
        "quantity": quantity,
        "price": price,
        "total": total,
    }

    _root_ref(_ref).child(
        f"sessions/{session_id}/bill_items/{product_id}"
    ).set(data)


# ---------------------------------------------------------------------------
# Event log sync
# ---------------------------------------------------------------------------

def append_event_log(
    session_id: int,
    timestamp: str,
    event_type: str,
    product_id: int,
    source: str | None = None,
    event_id: str | None = None,
    weight_change: float | None = None,
    *,
    _ref=None,
) -> None:
    """
    Append one event-log entry to Firebase at
    ``sessions/{session_id}/event_log/{firebase_push_key}/``.

    Uses Firebase's push-key mechanism so each event gets a unique,
    chronologically-ordered key generated by the SDK.  The SQLite ``log_id``
    and ``details`` fields are intentionally omitted per the approved schema.

    Parameters
    ----------
    session_id:
        Parent session identifier.
    timestamp:
        The ``timestamp`` string from ``session_log`` (SQLite
        ``datetime('now')`` format).
    event_type:
        ``"ADD"`` or ``"REMOVE"`` — the only two values currently written
        by ``billing.add_item()`` and ``billing.remove_item()``.
    product_id:
        The affected product.
    _ref:
        Injected root reference for testing.

    Raises
    ------
    Exception
        Any Firebase SDK exception is propagated to the caller.
    """
    data = {
        "timestamp": timestamp,
        "event_type": event_type,
        "product_id": product_id,
    }
    if source is not None:
        data["source"] = source
    if event_id is not None:
        data["event_id"] = event_id
    if weight_change is not None:
        data["weight_change"] = weight_change

    _root_ref(_ref).child(
        f"sessions/{session_id}/event_log"
    ).push(data)


# ---------------------------------------------------------------------------
# Convenience: remove a bill item when quantity reaches zero
# ---------------------------------------------------------------------------

def remove_bill_item(
    session_id: int,
    product_id: int,
    *,
    _ref=None,
) -> None:
    """
    Delete a bill-item node from Firebase when the product quantity reaches
    zero (mirrors ``billing.remove_item()`` deleting the row when
    ``quantity == 1``).

    Firebase path: ``sessions/{session_id}/bill_items/{product_id}/``

    Parameters
    ----------
    session_id:
        Parent session identifier.
    product_id:
        Product identifier for the node to delete.
    _ref:
        Injected root reference for testing.

    Raises
    ------
    Exception
        Any Firebase SDK exception is propagated to the caller.
    """
    _root_ref(_ref).child(
        f"sessions/{session_id}/bill_items/{product_id}"
    ).delete()
