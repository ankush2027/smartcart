"""
ProductResolver — converts YOLO detection results + weight evidence
+ CartState into a product resolution decision.

Responsibilities:
    - Accept Detection objects from a future YOLODetector.
    - Match detections against a product catalog by yolo_class.
    - Validate confidence against per-product thresholds.
    - Validate measured weight against expected product weight
      within WEIGHT_TOLERANCE_RATIO.
    - Return a Resolution that is either RESOLVED or UNRESOLVED,
      with an inspectable reason in every case.

Non-responsibilities:
    - Does NOT run YOLO or load any .pt model.
    - Does NOT import ultralytics.
    - Does NOT mutate CartState.
    - Does NOT access SQLite directly.
    - Does NOT perform adaptive threshold recalibration.
    - Does NOT implement CartSession.
"""

from __future__ import annotations

from dataclasses import dataclass

from edge.detection.cart_state import CartItem, CartState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ±15 % tolerance around expected_weight.
WEIGHT_TOLERANCE_RATIO: float = 0.15

#: Fallback confidence threshold when no per-class override is supplied.
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """
    Minimal detection record produced by a future YOLODetector.

    yolo_class   — class label (e.g. ``"Maggi_10"``).
    confidence   — model confidence in [0.0, 1.0].
    """
    yolo_class: str
    confidence: float


@dataclass
class ProductInfo:
    """
    Product metadata required for resolution.

    This is the catalog entry the resolver works against.
    In production it is built from the database; in tests it is
    constructed directly without any SQLite access.

    Fields
    ------
    product_id      — canonical database primary key.
    yolo_class      — YOLO class name (matches Detection.yolo_class).
    name            — human-readable product name.
    price           — unit price (informational).
    expected_weight — weight in grams of one unit.
    """
    product_id: int
    yolo_class: str
    name: str
    price: float
    expected_weight: float  # grams


@dataclass
class Resolution:
    """
    Result of a single product-resolution attempt.

    resolved    — True if the resolver reached a definitive answer.
    product_id  — set only when resolved is True.
    yolo_class  — set only when resolved is True.
    reason      — human-readable explanation in ALL cases.
                  When resolved is False the reason distinguishes:
                  ``"unknown class …"``, ``"confidence … below threshold …"``,
                  ``"weight … incompatible …"``, ``"multiple candidates …"``,
                  ``"removal ambiguous …"``, ``"cart is empty …"``,
                  ``"no cart item matches …"``.
    """
    resolved: bool
    product_id: int | None
    yolo_class: str | None
    reason: str


# ---------------------------------------------------------------------------
# ProductResolver
# ---------------------------------------------------------------------------

class ProductResolver:
    """
    Resolves ADD and REMOVE events into concrete product decisions.

    Parameters
    ----------
    catalog:
        List of :class:`ProductInfo` objects that represent every product
        the system may encounter.  In production this is populated from
        ``edge/database/product_repository.py``; in tests it is built
        from fake data.

    confidence_thresholds:
        Optional per-class confidence threshold overrides.
        ``{"Maggi_10": 0.7, "CocaCola": 0.8}``
        Classes not present in this mapping fall back to
        :data:`DEFAULT_CONFIDENCE_THRESHOLD`.

    Usage::

        catalog = [ProductInfo(1, "Maggi_10", "Maggi ₹10", 10.0, 75.0)]
        resolver = ProductResolver(catalog)

        resolution = resolver.resolve_add(
            detections=[Detection("Maggi_10", 0.92)],
            weight_change=75.0,
            cart_state=state,
        )
    """

    WEIGHT_TOLERANCE_RATIO: float = WEIGHT_TOLERANCE_RATIO

    def __init__(
        self,
        catalog: list[ProductInfo],
        confidence_thresholds: dict[str, float] | None = None,
    ) -> None:
        # Index catalog by yolo_class for O(1) lookup.
        self._catalog: dict[str, ProductInfo] = {
            p.yolo_class: p for p in catalog
        }
        self._thresholds: dict[str, float] = confidence_thresholds or {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def resolve_add(
        self,
        detections: list[Detection],
        weight_change: float,
        cart_state: CartState,
    ) -> Resolution:
        """
        Resolve an ADD event.

        Algorithm
        ---------
        For each detection:

        1. Look up the product by yolo_class — reject if unknown.
        2. Check confidence ≥ threshold — reject if too low.
        3. Check weight_change is within ±WEIGHT_TOLERANCE_RATIO of
           expected_weight — reject if incompatible.

        Return RESOLVED only when exactly one candidate passes all checks.
        Return UNRESOLVED (with reason) otherwise.

        CartState is accepted but not mutated.  It may be used in future
        for context (e.g. preventing duplicate-add anomalies).
        """
        candidates: list[ProductInfo] = []
        seen_classes: set[str] = set()
        rejection_reasons: list[str] = []

        for det in detections:
            yc = det.yolo_class

            # --- Step 1: class must be in catalog ---
            if yc not in self._catalog:
                rejection_reasons.append(f"unknown class '{yc}'")
                continue

            product = self._catalog[yc]
            threshold = self._threshold_for(yc)

            # --- Step 2: confidence check ---
            if det.confidence < threshold:
                rejection_reasons.append(
                    f"'{yc}' confidence {det.confidence:.3f} "
                    f"below threshold {threshold:.3f}"
                )
                continue

            # --- Step 3: weight compatibility ---
            if not self._weight_compatible(weight_change, product.expected_weight):
                lower = product.expected_weight * (1 - self.WEIGHT_TOLERANCE_RATIO)
                upper = product.expected_weight * (1 + self.WEIGHT_TOLERANCE_RATIO)
                rejection_reasons.append(
                    f"'{yc}' weight {weight_change:.1f}g not in "
                    f"[{lower:.1f}, {upper:.1f}]g"
                )
                continue

            # --- Passed all checks; de-duplicate by class ---
            if yc not in seen_classes:
                candidates.append(product)
                seen_classes.add(yc)

        # --- Decision ---
        if len(candidates) == 1:
            p = candidates[0]
            return Resolution(
                resolved=True,
                product_id=p.product_id,
                yolo_class=p.yolo_class,
                reason="ok",
            )

        if len(candidates) == 0:
            reason = (
                "; ".join(rejection_reasons)
                if rejection_reasons
                else "no detections provided"
            )
            return Resolution(
                resolved=False,
                product_id=None,
                yolo_class=None,
                reason=reason,
            )

        # Multiple candidates — unresolved
        names = [p.yolo_class for p in candidates]
        return Resolution(
            resolved=False,
            product_id=None,
            yolo_class=None,
            reason=f"multiple candidates: {names}",
        )

    def resolve_remove(
        self,
        detections: list[Detection],
        weight_change: float,
        cart_state: CartState,
    ) -> Resolution:
        """
        Resolve a REMOVE event using the CartState snapshot and weight only.

        The removed product may no longer appear in the camera frame, so the
        resolver treats the CartState BEFORE snapshot as the authoritative
        source of candidates and uses the absolute weight change as the sole
        physical evidence.

        Algorithm
        ---------
        1. measured = abs(weight_change).
        2. For each CartItem in cart_state.snapshot():
           a. Skip if its yolo_class is not in the catalog.
           b. Skip if measured weight is outside ±WEIGHT_TOLERANCE_RATIO of
              the product's expected_weight.
        3. Exactly one candidate → RESOLVED.
           Zero or multiple → UNRESOLVED.

        Detection data is accepted for API compatibility but is NOT used to
        filter candidates.  Camera visibility after a removal is not a
        reliable signal — a class may still be visible because multiple units
        remain, or because detection fired on an adjacent frame.  If the
        weight evidence alone is insufficient to identify the removed product
        uniquely, this method returns UNRESOLVED rather than guessing.

        Limitation: when two products in the catalog have expected weights
        within each other's tolerance bands, weight alone cannot disambiguate
        them.  Detection-based disambiguation for REMOVE is deferred to a
        future task.

        CartState is NOT mutated.  CartState mutation is CartSession's job.
        """
        measured = abs(weight_change)

        before_items: list[CartItem] = cart_state.snapshot()

        if not before_items:
            return Resolution(
                resolved=False,
                product_id=None,
                yolo_class=None,
                reason="cart is empty, nothing to remove",
            )

        candidates: list[CartItem] = []

        for item in before_items:
            yc = item.yolo_class

            # Must be a known product.
            if yc not in self._catalog:
                continue

            product = self._catalog[yc]

            # Weight must be compatible.
            if not self._weight_compatible(measured, product.expected_weight):
                continue

            candidates.append(item)

        if len(candidates) == 1:
            item = candidates[0]
            return Resolution(
                resolved=True,
                product_id=item.product_id,
                yolo_class=item.yolo_class,
                reason="ok",
            )

        if len(candidates) == 0:
            return Resolution(
                resolved=False,
                product_id=None,
                yolo_class=None,
                reason="no cart item matches the measured weight",
            )

        # Multiple candidates — ambiguous removal.
        names = [item.yolo_class for item in candidates]
        return Resolution(
            resolved=False,
            product_id=None,
            yolo_class=None,
            reason=f"removal ambiguous: multiple candidates {names}",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _threshold_for(self, yolo_class: str) -> float:
        """Return the confidence threshold for *yolo_class*."""
        return self._thresholds.get(yolo_class, DEFAULT_CONFIDENCE_THRESHOLD)

    def _weight_compatible(self, measured: float, expected: float) -> bool:
        """
        Return True when *measured* falls within ±WEIGHT_TOLERANCE_RATIO
        of *expected*.

        Boundary values are included (≤ / ≥).
        """
        lower = expected * (1.0 - self.WEIGHT_TOLERANCE_RATIO)
        upper = expected * (1.0 + self.WEIGHT_TOLERANCE_RATIO)
        return lower <= measured <= upper
