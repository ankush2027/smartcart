"""
CartSession — application-level coordinator for SmartCart.

Responsibilities:
    - Accept a confirmed ADD or REMOVE event (detections + weight_change).
    - Delegate product resolution to an injected ProductResolver.
    - Apply the resolution to CartState (in-memory) and billing (SQLite).
    - Return an inspectable SessionResult for every call.

Non-responsibilities:
    - Does NOT perform YOLO inference.
    - Does NOT perform event detection (camera, weight sensor).
    - Does NOT contain MOG2, HX711, or EventOrchestrator logic.
    - Does NOT implement adaptive thresholding, Firebase, or Flutter.
    - Does NOT duplicate ProductResolver logic.

Consistency model
-----------------
ADD flow
    1. Resolve product (ProductResolver.resolve_add).
    2. If UNRESOLVED → return without modifying anything.
    3. Call billing.add_item() first.
    4. If billing fails → return failure; CartState is still clean.
    5. Call CartState.add() (guaranteed to succeed).

REMOVE flow
    1. Resolve product (ProductResolver.resolve_remove, reads CartState BEFORE).
    2. If UNRESOLVED → return without modifying anything.
    3. Call CartState.remove() first.
    4. If CartState.remove() returns False → product not in cart; billing unchanged.
    5. Call billing.remove_item().
    6. If billing fails → rollback CartState (re-add the item); return failure.

Limitation
----------
CartState (in-memory) and billing (SQLite) are separate stores with no shared
transaction boundary.  The rollback in REMOVE step 6 is best-effort Python and
is safe as long as CartState.add() does not raise (it cannot with the current
pure-Python implementation).  If strict atomic rollback across both stores
becomes a requirement, CartState should be migrated to the same SQLite database
so both mutations can occur inside one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from edge.billing.billing import (
    add_item as _billing_add,
    complete_session as _billing_complete,
    create_session as _billing_create_session,
    get_bill as _billing_get_bill,
    get_session_log as _billing_get_log,
    remove_item as _billing_remove,
)
from edge.detection.cart_state import CartState
from edge.detection.product_resolver import Detection, ProductResolver


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

    Usage::

        resolver = ProductResolver(catalog)
        session  = CartSession(resolver)

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
    ) -> None:
        self._resolver: ProductResolver = resolver
        self._cart_state: CartState = CartState()
        self._session_id: int = (
            session_id
            if session_id is not None
            else _billing_create_session()
        )

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
        """
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
        ok = _billing_add(self._session_id, resolution.product_id)
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
        self._cart_state.add(resolution.product_id, resolution.yolo_class)

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
        """
        # resolve_remove reads CartState.snapshot() internally.
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
        ok = _billing_remove(self._session_id, resolution.product_id)
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
        and ``total_amount``.
        """
        return _billing_complete(self._session_id)

    def get_session_log(self) -> list:
        """Return the ordered ADD/REMOVE event log for this session."""
        return _billing_get_log(self._session_id)

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

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
