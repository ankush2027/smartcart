"""
CartSession — application-level coordinator for SmartCart.

Responsibilities:
    - Accept a confirmed ADD or REMOVE event (detections + weight_change).
    - Delegate product resolution to an injected ProductResolver.
    - Apply the resolution to CartState (in-memory) and billing (SQLite).
    - After every successful SQLite mutation, synchronize the result to
      Firebase via the injected Firebase reference.
    - Support cart_id and customer_uid session ownership.
    - Return an inspectable SessionResult for every call.

Non-responsibilities:
    - Does NOT perform YOLO inference.
    - Does NOT perform event detection (camera, weight sensor).
    - Does NOT contain MOG2, HX711, or EventOrchestrator logic.
    - Does NOT implement adaptive thresholding.
    - Does NOT duplicate ProductResolver logic.
    - Does NOT duplicate billing or sync logic.

Consistency model
-----------------
ADD flow
    1. Resolve product (ProductResolver.resolve_add).
    2. If UNRESOLVED → return without modifying anything.
    3. Call billing.add_item() first.
    4. If billing fails → return failure; CartState is still clean.
    5. Call CartState.add() (guaranteed to succeed).
    6. Synchronize to Firebase (bill item + session total + event log).
    7. Firebase failure is logged and re-raised; it does NOT roll back SQLite.

REMOVE flow
    1. Resolve product (ProductResolver.resolve_remove, reads CartState BEFORE).
    2. If UNRESOLVED → return without modifying anything.
    3. Call CartState.remove() first.
    4. If CartState.remove() returns False → product not in cart; billing unchanged.
    5. Call billing.remove_item().
    6. If billing fails → rollback CartState (re-add the item); return failure.
    7. Synchronize to Firebase (bill item updated or deleted + session total + event log).
    8. Firebase failure is logged and re-raised; it does NOT roll back SQLite.

Firebase synchronization point
-------------------------------
Firebase sync occurs **after** the corresponding SQLite operation succeeds.
This is the single authoritative synchronization point for all ADD/REMOVE
operations.  The sync helpers in ``edge.firebase.sync`` are called directly
from this class; they are never called from inside ``edge.billing``.

SQLite-authoritative rule
-------------------------
SQLite success ≠ Firebase success.  They are independent systems.  A Firebase
failure surfaces as an exception from the sync call but does NOT cause a
SQLite rollback.  The cart continues to bill correctly even when Firebase is
unreachable.

Limitation
----------
CartState (in-memory) and billing (SQLite) are separate stores with no shared
transaction boundary.  The rollback in REMOVE step 6 is best-effort Python and
is safe as long as CartState.add() does not raise.  If strict atomic rollback
across both stores becomes a requirement, CartState should be migrated to the
same SQLite database so both mutations can occur inside one transaction.

Future: offline queue
---------------------
Firebase sync is structured so an offline queue can be added later without
rewriting billing.  The sync call in :meth:`_sync_after_add` and
:meth:`_sync_after_remove` is the single point where a queue-and-retry layer
would be inserted.

Session ownership model
-----------------------
Each CartSession carries an optional ``cart_id`` (physical cart identifier)
and ``customer_uid`` (Firebase Auth UID).  These are persisted to SQLite
via ``billing`` and included in every Firebase session sync.

    Cart  →  Active shopping session  →  Customer UID

A session is considered active when ``end_time == null`` in Firebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from edge.billing.billing import (
    InactiveSessionError,
    CheckoutFrozenError,
    StaleRuntimeError,
    hydrate_cart as _billing_state_rows,
    add_runtime_item as _billing_add,
    assign_customer_to_session as _billing_assign_customer,
    complete_session as _billing_complete,
    create_session as _billing_create_session,
    get_bill as _billing_get_bill,
    get_bill_item as _billing_get_bill_item,
    get_latest_log_entry as _billing_get_latest_log,
    get_session_log as _billing_get_log,
    get_session_meta as _billing_get_session_meta,
    remove_runtime_item as _billing_remove,
    set_session_cart_id as _billing_set_cart_id,
)
from edge.detection.cart_state import CartState
from edge.detection.product_resolver import Detection, ProductResolver
from edge.firebase import sync as _firebase_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SessionResult:
    """
    Result of a :class:`CartSession` event-processing call.

    Fields
    ------
    success
        True when BOTH CartState and billing were updated successfully.
    resolved
        True when the ProductResolver identified the product.
        A result may be *resolved but not successful* when billing fails
        after product resolution.
    product_id
        Set only when ``resolved`` is True.
    yolo_class
        Set only when ``resolved`` is True.
    reason
        Human-readable explanation in every case:
        ``"ok"`` on full success; a description of the failure otherwise.
    """
    success: bool
    resolved: bool
    product_id: int | None
    yolo_class: str | None
    reason: str


# ---------------------------------------------------------------------------
# CartSession
# ---------------------------------------------------------------------------

class CartSession:
    """
    Application-level coordinator for one shopping-cart session.

    Each instance owns:
    - An independent :class:`~edge.detection.cart_state.CartState`.
    - A billing ``session_id`` created in the SQLite ``sessions`` table.
    - An optional Firebase reference used for synchronization.

    It does NOT share state with any other CartSession instance and does NOT
    use module-level globals.

    Parameters
    ----------
    resolver:
        A pre-constructed :class:`~edge.detection.product_resolver.ProductResolver`
        whose catalog matches the products in the database.
    session_id:
        Optional existing billing session ID.  Pass an integer to attach to
        a pre-existing billing session (e.g. for testing).  If ``None``
        (the default), a new session row is created via
        ``billing.create_session()``.
    cart_id:
        Optional stable physical cart identifier (e.g. QR code value).
        When provided, it is persisted to the SQLite ``sessions`` row
        immediately and included in every Firebase session sync.
    firebase_ref:
        Optional root Firebase Realtime Database reference.  When provided,
        all successful billing mutations are synchronized to Firebase.
        When ``None``, Firebase synchronization is silently skipped —
        useful for tests that do not need Firebase.  Pass a fake reference
        object to test sync behavior without a real Firebase project.

    Usage::

        resolver = ProductResolver(catalog)
        session  = CartSession(resolver, firebase_ref=my_firebase_root_ref)

        result = session.process_add(
            detections=[Detection(\"Maggi_10\", 0.92)],
            weight_change=75.0,
        )
        if result.success:
            print(\"Added:\", result.yolo_class)
    """

    def __init__(
        self,
        resolver: ProductResolver,
        session_id: int | None = None,
        *,
        cart_id: str | None = None,
        firebase_ref=None,
    ) -> None:
        self._resolver: ProductResolver = resolver
        self._cart_state: CartState = CartState()
        self._firebase_ref = firebase_ref
        self._last_mutation_committed = False

        if session_id is not None:
            self._session_id: int = session_id
            rows, self._checkout_generation = _billing_state_rows(session_id)
            seen = set()
            for product_id, yolo_class, quantity in rows:
                if (product_id in seen or not isinstance(yolo_class, str)
                        or not yolo_class.strip() or not isinstance(quantity, int)
                        or quantity <= 0):
                    raise ValueError("Cannot resume invalid SQLite cart state")
                seen.add(product_id)
                for _ in range(quantity):
                    self._cart_state.add(product_id, yolo_class)

        else:
            self._session_id = _billing_create_session(cart_id=cart_id)
            _, self._checkout_generation = _billing_state_rows(self._session_id)
            if cart_id is not None and session_id is None:
                # cart_id already stored by create_session(); no extra write needed.
                pass

        # Persist cart_id when a pre-existing session_id was supplied but
        # cart_id was also given — update the existing row.
        if session_id is not None and cart_id is not None:
            _billing_set_cart_id(self._session_id, cart_id)

    # ------------------------------------------------------------------
    # Session ownership
    # ------------------------------------------------------------------

    def assign_customer(self, customer_uid: str) -> None:
        """
        Assign an authenticated customer's Firebase UID to this session.

        Persists ``customer_uid`` to SQLite and synchronizes the session
        metadata to Firebase (if a Firebase reference is configured).

        Parameters
        ----------
        customer_uid:
            The Firebase Auth UID of the authenticated customer.  This
            value is used by Firebase security rules to restrict customer
            read access to their own session only.
        """
        _billing_assign_customer(self._session_id, customer_uid)
        self._sync_session_meta()

    # ------------------------------------------------------------------
    # Core event processing
    # ------------------------------------------------------------------

    def process_add(
        self,
        detections: list[Detection],
        weight_change: float,
    ) -> SessionResult:
        """
        Process a confirmed ADD event.

        Parameters
        ----------
        detections:
            Detection objects produced by a (future) YOLODetector.
        weight_change:
            Positive weight delta in grams from the weight pipeline.

        Returns
        -------
        SessionResult
            ``success=True`` when both CartState and billing are updated.
            Firebase sync is attempted after SQLite success; Firebase
            failures raise after commit; inspect ``last_mutation_committed``.
        """
        self._last_mutation_committed = False
        meta = _billing_get_session_meta(self._session_id)
        if meta is None or meta['end_time'] is not None:
            return SessionResult(False, False, None, None, "Session is missing or completed")

        resolution = self._resolver.resolve_add(
            detections=detections,
            weight_change=weight_change,
            cart_state=self._cart_state,
        )

        if not resolution.resolved:
            return SessionResult(
                success=False,
                resolved=False,
                product_id=None,
                yolo_class=None,
                reason=resolution.reason,
            )

        # Billing first: ensures CartState is never ahead of the
        # persistent record if the product is missing from the DB.
        try:
            ok = _billing_add(self._session_id, resolution.product_id,
                              expected_generation=self._checkout_generation)
        except CheckoutFrozenError:
            return SessionResult(False, True, resolution.product_id, resolution.yolo_class,
                                 "Session checkout is frozen")
        except StaleRuntimeError as error:
            return SessionResult(False, True, resolution.product_id, resolution.yolo_class, str(error))
        except InactiveSessionError:
            return SessionResult(False, True, resolution.product_id, resolution.yolo_class,
                                 "Session is missing or completed")
        if not ok:
            return SessionResult(
                success=False,
                resolved=True,
                product_id=resolution.product_id,
                yolo_class=resolution.yolo_class,
                reason=(
                    f"billing.add_item failed for product_id="
                    f"{resolution.product_id} (product absent from DB?); "
                    f"CartState unchanged"
                ),
            )

        # CartState.add() has no failure path — safe to call after billing.
        self._last_mutation_committed = True
        self._cart_state.add(resolution.product_id, resolution.yolo_class)

        # Firebase sync: after both SQLite and CartState succeed.
        # Firebase failure is logged but does NOT roll back SQLite.
        self._sync_after_add(resolution.product_id)

        return SessionResult(
            success=True,
            resolved=True,
            product_id=resolution.product_id,
            yolo_class=resolution.yolo_class,
            reason="ok",
        )

    def process_remove(
        self,
        detections: list[Detection],
        weight_change: float,
    ) -> SessionResult:
        """
        Process a confirmed REMOVE event.

        Parameters
        ----------
        detections:
            Detection objects from the current frame (may be empty if the
            product is no longer visible).
        weight_change:
            Negative weight delta in grams from the weight pipeline.

        Returns
        -------
        SessionResult
            ``success=True`` when both CartState and billing are updated.
            Firebase sync is attempted after SQLite success; Firebase
            failures raise after commit; inspect ``last_mutation_committed``.
        """
        # resolve_remove reads CartState.snapshot() internally.
        self._last_mutation_committed = False
        meta = _billing_get_session_meta(self._session_id)
        if meta is None or meta['end_time'] is not None:
            return SessionResult(False, False, None, None, "Session is missing or completed")

        resolution = self._resolver.resolve_remove(
            detections=detections,
            weight_change=weight_change,
            cart_state=self._cart_state,
        )

        if not resolution.resolved:
            return SessionResult(
                success=False,
                resolved=False,
                product_id=None,
                yolo_class=None,
                reason=resolution.reason,
            )

        # CartState first: guard against the product not actually being
        # in the cart (e.g. resolver found it via weight but state is stale).
        removed = self._cart_state.remove(resolution.product_id)
        if not removed:
            return SessionResult(
                success=False,
                resolved=True,
                product_id=resolution.product_id,
                yolo_class=resolution.yolo_class,
                reason=(
                    f"product_id={resolution.product_id} not found in CartState "
                    f"at removal time; billing unchanged"
                ),
            )

        # Billing: decrement bill item.
        try:
            ok = _billing_remove(self._session_id, resolution.product_id,
                                 expected_generation=self._checkout_generation)
        except CheckoutFrozenError:
            self._cart_state.add(resolution.product_id, resolution.yolo_class)
            return SessionResult(False, True, resolution.product_id, resolution.yolo_class,
                                 "Session checkout is frozen")
        except StaleRuntimeError as error:
            self._cart_state.add(resolution.product_id, resolution.yolo_class)
            return SessionResult(False, True, resolution.product_id, resolution.yolo_class, str(error))
        except Exception:
            self._cart_state.add(resolution.product_id, resolution.yolo_class)
            raise
        if not ok:
            # Billing failed after CartState was already decremented.
            # Roll back CartState to restore consistency.
            self._cart_state.add(resolution.product_id, resolution.yolo_class)
            return SessionResult(
                success=False,
                resolved=True,
                product_id=resolution.product_id,
                yolo_class=resolution.yolo_class,
                reason=(
                    f"billing.remove_item failed for product_id="
                    f"{resolution.product_id} (item absent from bill?); "
                    f"CartState rolled back"
                ),
            )

        self._last_mutation_committed = True
        # Firebase failure may raise after commit. Never repeat the mutation.
        self._sync_after_remove(resolution.product_id)

        return SessionResult(
            success=True,
            resolved=True,
            product_id=resolution.product_id,
            yolo_class=resolution.yolo_class,
            reason="ok",
        )

    # ------------------------------------------------------------------
    # Billing helpers (thin pass-throughs)
    # ------------------------------------------------------------------

    def get_bill(self) -> list:
        """
        Return the current bill as a list of tuples.

        Each tuple: ``(product_id, name, quantity, price, total)``.
        """
        return _billing_get_bill(self._session_id)

    def complete(self) -> float:
        """
        Mark the session as complete and return the final total.

        After this call the ``sessions`` row is updated with ``end_time``
        and ``total_amount``.  The final session state is then synchronized
        to Firebase (if a Firebase reference is configured).

        Returns
        -------
        float
            Final total.
        """
        total = _billing_complete(self._session_id)
        # Sync the completed session state (end_time + final total).
        self._sync_session_meta()
        return total

    def get_session_log(self) -> list:
        """Return the ordered ADD/REMOVE event log for this session."""
        return _billing_get_log(self._session_id)

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def last_mutation_committed(self) -> bool:
        """Outcome of the last process call, including calls that raised.

        Single-owner API: inspect immediately after dispatch. True means the
        local bill/log/total transaction committed; never replay that mutation.
        """
        return self._last_mutation_committed

    @property
    def session_id(self) -> int:
        """The billing ``session_id`` for this cart session."""
        return self._session_id

    @property
    def cart_state(self) -> CartState:
        """
        The current in-memory CartState.

        Callers must not mutate this object directly; all mutations must
        go through :meth:`process_add` or :meth:`process_remove`.
        """
        return self._cart_state

    # ------------------------------------------------------------------
    # Firebase synchronization (private)
    # ------------------------------------------------------------------

    def _sync_after_add(self, product_id: int) -> None:
        """
        Synchronize Firebase after a successful ADD billing mutation.

        Sequence:
          1. Fetch the updated bill item from SQLite.
          2. sync_bill_item  → Firebase bill_items/{product_id}
          3. sync_session    → Firebase sessions/{session_id} (total_amount)
          4. append_event_log → Firebase event_log/{push_key}

        Firebase failure is logged and re-raised so the caller can decide
        how to handle it.  SQLite state is never rolled back here.
        """
        if self._firebase_ref is None:
            return

        try:
            bill_item = _billing_get_bill_item(self._session_id, product_id)
            if bill_item is None:
                logger.warning(
                    "CartSession._sync_after_add: bill item for session=%d "
                    "product=%d not found in SQLite after add; skipping sync",
                    self._session_id, product_id,
                )
                return

            _pid, name, quantity, price, total = bill_item

            _firebase_sync.sync_bill_item(
                session_id=self._session_id,
                product_id=product_id,
                product_name=name,
                quantity=quantity,
                price=price,
                total=total,
                _ref=self._firebase_ref,
            )

            self._sync_session_meta()

            log_entry = _billing_get_latest_log(self._session_id)
            if log_entry is not None:
                _firebase_sync.append_event_log(
                    session_id=self._session_id,
                    timestamp=log_entry[2],   # timestamp column
                    event_type=log_entry[3],  # event_type column
                    product_id=log_entry[4],  # product_id column
                    _ref=self._firebase_ref,
                )

        except Exception:
            logger.exception(
                "CartSession._sync_after_add: Firebase sync failed for "
                "session=%d product=%d — SQLite state is unchanged",
                self._session_id, product_id,
            )
            raise

    def _sync_after_remove(self, product_id: int) -> None:
        """
        Synchronize Firebase after a successful REMOVE billing mutation.

        Sequence:
          1. Check whether the bill item still exists in SQLite.
          2a. If it still exists (quantity > 0): sync_bill_item (update).
          2b. If it was deleted (quantity reached 0): remove_bill_item (delete).
          3. sync_session → Firebase sessions/{session_id} (total_amount).
          4. append_event_log → Firebase event_log/{push_key}.

        Firebase failure is logged and re-raised so the caller can decide
        how to handle it.  SQLite state is never rolled back here.
        """
        if self._firebase_ref is None:
            return

        try:
            bill_item = _billing_get_bill_item(self._session_id, product_id)

            if bill_item is not None:
                _pid, name, quantity, price, total = bill_item
                _firebase_sync.sync_bill_item(
                    session_id=self._session_id,
                    product_id=product_id,
                    product_name=name,
                    quantity=quantity,
                    price=price,
                    total=total,
                    _ref=self._firebase_ref,
                )
            else:
                # Quantity reached zero — SQLite row was deleted; mirror in Firebase.
                _firebase_sync.remove_bill_item(
                    session_id=self._session_id,
                    product_id=product_id,
                    _ref=self._firebase_ref,
                )

            self._sync_session_meta()

            log_entry = _billing_get_latest_log(self._session_id)
            if log_entry is not None:
                _firebase_sync.append_event_log(
                    session_id=self._session_id,
                    timestamp=log_entry[2],
                    event_type=log_entry[3],
                    product_id=log_entry[4],
                    _ref=self._firebase_ref,
                )

        except Exception:
            logger.exception(
                "CartSession._sync_after_remove: Firebase sync failed for "
                "session=%d product=%d — SQLite state is unchanged",
                self._session_id, product_id,
            )
            raise

    def _sync_session_meta(self) -> None:
        """
        Synchronize the current session metadata (total_amount, end_time,
        cart_id, customer_uid) to Firebase.

        Called after every successful billing mutation and after
        :meth:`assign_customer`.

        Billing commits bill items, log and total together. Read the committed
        metadata directly; synchronization must never rewrite SQLite totals.

        Silently skips synchronization when no Firebase reference is
        configured.  Propagates Firebase exceptions to the caller.
        """
        if self._firebase_ref is None:
            return

        meta = _billing_get_session_meta(self._session_id)
        if meta is None:
            logger.warning(
                "CartSession._sync_session_meta: session %d not found in "
                "SQLite; skipping Firebase session sync",
                self._session_id,
            )
            return

        _firebase_sync.sync_session(
            session_id=meta["session_id"],
            start_time=meta["start_time"],
            end_time=meta["end_time"],
            total_amount=meta["total_amount"],
            cart_id=meta["cart_id"],
            customer_uid=meta["customer_uid"],
            _ref=self._firebase_ref,
        )
