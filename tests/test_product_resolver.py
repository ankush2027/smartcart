"""
Tests for edge.detection.product_resolver — ProductResolver.

All tests use in-memory ProductInfo / Detection objects.
No YOLO model, no SQLite, no camera, no network.

Required cases (per task spec):
 1.  ADD resolves one clear matching product.
 2.  ADD rejects low confidence.
 3.  ADD rejects weight mismatch.
 4.  ADD rejects unknown YOLO class.
 5.  ADD returns unresolved when multiple candidates are equally plausible.
 6.  Weight tolerance exactly at lower boundary → resolved.
 7.  Weight tolerance exactly at upper boundary → resolved.
 8.  Weight outside tolerance → unresolved.
 9.  REMOVE resolves a clear product when evidence is sufficient.
10.  REMOVE does not blindly remove an absent/ambiguous product.
11.  REMOVE respects CartState quantity.
12.  Duplicate product quantity is handled correctly.
13.  Resolver does not mutate CartState.
14.  Unresolved result contains a useful reason.
15.  Existing project regression tests continue passing.

Explicit REMOVE cases from the architecture review:
 A.  qty=2, class still visible → RESOLVED (weight wins).
 B.  qty=1, class gone, weight matches → RESOLVED.
 C.  Two products with overlapping weights → UNRESOLVED/ambiguous.
 D.  No weight match → UNRESOLVED.
 E.  Duplicate quantity does not create duplicate candidates.
"""

# pyrefly: ignore [missing-import]
import pytest

from edge.detection.cart_state import CartState
from edge.detection.product_resolver import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    WEIGHT_TOLERANCE_RATIO,
    Detection,
    ProductInfo,
    ProductResolver,
    Resolution,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_product(
    product_id: int,
    yolo_class: str,
    *,
    name: str = "Test Product",
    price: float = 10.0,
    expected_weight: float = 100.0,
) -> ProductInfo:
    return ProductInfo(
        product_id=product_id,
        yolo_class=yolo_class,
        name=name,
        price=price,
        expected_weight=expected_weight,
    )


def _det(yolo_class: str, confidence: float = 0.9) -> Detection:
    return Detection(yolo_class=yolo_class, confidence=confidence)


def _resolver(*products: ProductInfo, thresholds: dict | None = None) -> ProductResolver:
    return ProductResolver(list(products), confidence_thresholds=thresholds)


def _cart(*entries) -> CartState:
    """Build CartState from (product_id, yolo_class [, quantity]) tuples."""
    state = CartState()
    for entry in entries:
        pid, yc = entry[0], entry[1]
        qty = entry[2] if len(entry) > 2 else 1
        for _ in range(qty):
            state.add(pid, yc)
    return state


# ---------------------------------------------------------------------------
# Standard catalog used across several tests
# ---------------------------------------------------------------------------

MAGGI_10  = _make_product(1, "Maggi_10",  name="Maggi ₹10",  price=10.0,  expected_weight=75.0)
MAGGI_20  = _make_product(2, "Maggi_20",  name="Maggi ₹20",  price=20.0,  expected_weight=100.0)
COCA_COLA = _make_product(3, "CocaCola",  name="Coca Cola",  price=35.0,  expected_weight=330.0)
LAYS      = _make_product(4, "Lays",      name="Lays Chips", price=20.0,  expected_weight=50.0)


# ===========================================================================
# 1. ADD resolves one clear matching product
# ===========================================================================

def test_add_resolves_single_clear_product():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.92)],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1
    assert result.yolo_class == "Maggi_10"
    assert result.reason == "ok"


# ===========================================================================
# 2. ADD rejects low confidence
# ===========================================================================

def test_add_rejects_low_confidence():
    resolver = _resolver(MAGGI_10, thresholds={"Maggi_10": 0.8})
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.5)],   # below threshold 0.8
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert "confidence" in result.reason
    assert "threshold" in result.reason


def test_add_rejects_confidence_just_below_threshold():
    """Confidence of 0.699 must fail a threshold of 0.70."""
    resolver = _resolver(MAGGI_10, thresholds={"Maggi_10": 0.70})
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.699)],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is False


def test_add_accepts_confidence_exactly_at_threshold():
    """Confidence of exactly 0.70 must pass a threshold of 0.70."""
    resolver = _resolver(MAGGI_10, thresholds={"Maggi_10": 0.70})
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.70)],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is True


# ===========================================================================
# 3. ADD rejects weight mismatch
# ===========================================================================

def test_add_rejects_weight_mismatch():
    resolver = _resolver(MAGGI_10)   # expected_weight = 75 g
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.9)],
        weight_change=200.0,          # way outside ±15 %
        cart_state=state,
    )

    assert result.resolved is False
    assert "weight" in result.reason


# ===========================================================================
# 4. ADD rejects unknown YOLO class
# ===========================================================================

def test_add_rejects_unknown_yolo_class():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("UFO_Product", 0.95)],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert "unknown" in result.reason


# ===========================================================================
# 5. ADD returns unresolved when multiple candidates are equally plausible
# ===========================================================================

def test_add_unresolved_multiple_candidates():
    """
    Maggi_10 (75 g) and Lays (50 g) both match separate detections,
    both have good confidence, but their weights are different so only
    the matching one should pass weight check.

    To force a genuine multi-candidate scenario we create two products
    with the *same* expected weight.
    """
    product_a = _make_product(10, "ProductA", expected_weight=100.0)
    product_b = _make_product(11, "ProductB", expected_weight=100.0)
    resolver  = _resolver(product_a, product_b)
    state     = CartState()

    result = resolver.resolve_add(
        detections=[
            _det("ProductA", 0.9),
            _det("ProductB", 0.9),
        ],
        weight_change=100.0,   # compatible with both
        cart_state=state,
    )

    assert result.resolved is False
    assert "multiple" in result.reason


# ===========================================================================
# 6. Weight tolerance exactly at lower boundary → resolved
# ===========================================================================

def test_add_weight_exactly_at_lower_boundary():
    """
    WEIGHT_TOLERANCE_RATIO = 0.15
    expected = 100 g → lower boundary = 85 g.
    Measured = 85.0 g must be RESOLVED.
    """
    product  = _make_product(1, "Item", expected_weight=100.0)
    resolver = _resolver(product)
    state    = CartState()

    lower = 100.0 * (1.0 - WEIGHT_TOLERANCE_RATIO)   # 85.0

    result = resolver.resolve_add(
        detections=[_det("Item", 0.9)],
        weight_change=lower,
        cart_state=state,
    )

    assert result.resolved is True, f"lower boundary {lower}g should resolve"


# ===========================================================================
# 7. Weight tolerance exactly at upper boundary → resolved
# ===========================================================================

def test_add_weight_exactly_at_upper_boundary():
    """
    expected = 100 g → upper boundary = 115 g.
    Measured = 115.0 g must be RESOLVED.
    """
    product  = _make_product(1, "Item", expected_weight=100.0)
    resolver = _resolver(product)
    state    = CartState()

    upper = 100.0 * (1.0 + WEIGHT_TOLERANCE_RATIO)   # 115.0

    result = resolver.resolve_add(
        detections=[_det("Item", 0.9)],
        weight_change=upper,
        cart_state=state,
    )

    assert result.resolved is True, f"upper boundary {upper}g should resolve"


# ===========================================================================
# 8. Weight outside tolerance → unresolved
# ===========================================================================

def test_add_weight_just_below_lower_boundary():
    product  = _make_product(1, "Item", expected_weight=100.0)
    resolver = _resolver(product)
    state    = CartState()

    just_below = 100.0 * (1.0 - WEIGHT_TOLERANCE_RATIO) - 0.001  # 84.999

    result = resolver.resolve_add(
        detections=[_det("Item", 0.9)],
        weight_change=just_below,
        cart_state=state,
    )

    assert result.resolved is False


def test_add_weight_just_above_upper_boundary():
    product  = _make_product(1, "Item", expected_weight=100.0)
    resolver = _resolver(product)
    state    = CartState()

    just_above = 100.0 * (1.0 + WEIGHT_TOLERANCE_RATIO) + 0.001  # 115.001

    result = resolver.resolve_add(
        detections=[_det("Item", 0.9)],
        weight_change=just_above,
        cart_state=state,
    )

    assert result.resolved is False


# ===========================================================================
# 9. REMOVE resolves a clear product when evidence is sufficient
# ===========================================================================

def test_remove_resolves_single_clear_product():
    """
    Cart has only Maggi_10.  Item is gone from frame.
    Weight drop matches expected_weight.  Must resolve.
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10"))

    result = resolver.resolve_remove(
        detections=[],      # item no longer visible
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1
    assert result.yolo_class == "Maggi_10"


def test_remove_resolves_when_other_items_still_visible():
    """
    Cart has Maggi_10 and CocaCola.
    Customer removes Maggi_10 (75 g drop).
    CocaCola is still visible in detections.
    Resolver must pick Maggi_10, not CocaCola.
    """
    resolver = _resolver(MAGGI_10, COCA_COLA)
    state    = _cart((1, "Maggi_10"), (3, "CocaCola"))

    result = resolver.resolve_remove(
        detections=[_det("CocaCola", 0.95)],   # CocaCola still there
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1
    assert result.yolo_class == "Maggi_10"


# ===========================================================================
# 10. REMOVE does not blindly remove an absent/ambiguous product
# ===========================================================================

def test_remove_unresolved_when_ambiguous():
    """
    Cart has two products with similar weights.
    Both could explain the measured weight drop.
    Resolver must return UNRESOLVED.
    """
    product_a = _make_product(10, "ProductA", expected_weight=100.0)
    product_b = _make_product(11, "ProductB", expected_weight=100.0)
    resolver  = _resolver(product_a, product_b)
    state     = _cart((10, "ProductA"), (11, "ProductB"))

    result = resolver.resolve_remove(
        detections=[],      # neither visible
        weight_change=-100.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert "ambiguous" in result.reason


def test_remove_unresolved_empty_cart():
    resolver = _resolver(MAGGI_10)
    state    = CartState()       # empty

    result = resolver.resolve_remove(
        detections=[],
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert "empty" in result.reason


def test_remove_unresolved_weight_matches_nothing():
    resolver = _resolver(MAGGI_10)  # expected 75 g
    state    = _cart((1, "Maggi_10"))

    result = resolver.resolve_remove(
        detections=[],
        weight_change=-999.0,   # no product weighs this
        cart_state=state,
    )

    assert result.resolved is False
    assert "matches" in result.reason


# ===========================================================================
# 11. REMOVE respects CartState quantity
# ===========================================================================

def test_remove_resolves_when_class_still_visible_but_quantity_gt_1():
    """
    Cart has Maggi_10 × 2.
    Customer removes one; the other is still visible in detections.
    Resolver should still identify Maggi_10 as the removed product.
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10", 2))   # quantity = 2

    result = resolver.resolve_remove(
        detections=[_det("Maggi_10", 0.9)],   # one still in frame
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1


def test_remove_resolves_by_weight_regardless_of_visibility():
    """
    CORRECTED (was: test_remove_skips_class_when_quantity_is_1_and_still_visible)

    The resolver no longer uses camera visibility as a filter for REMOVE.
    Weight is the sole physical evidence.

    Cart: Maggi_10 (75 g, qty=1) + CocaCola (330 g, qty=1).
    Weight drop: 75 g → matches only Maggi_10.
    Maggi_10 still detected in frame → IRRELEVANT to resolution.

    Expected: RESOLVED with Maggi_10 because weight uniquely identifies it.
    """
    resolver = _resolver(MAGGI_10, COCA_COLA)
    state    = _cart((1, "Maggi_10"), (3, "CocaCola"))

    result = resolver.resolve_remove(
        detections=[_det("Maggi_10", 0.95)],   # still visible — ignored
        weight_change=-75.0,
        cart_state=state,
    )

    # Weight uniquely identifies Maggi_10 — visibility does not block resolution.
    assert result.resolved is True
    assert result.product_id == 1
    assert result.yolo_class == "Maggi_10"


# ===========================================================================
# Architecture review CASES A – E
# ===========================================================================

def test_remove_case_a_qty2_still_visible_resolves():
    """
    CASE A (architecture review):
    Cart: Maggi_10 × 2.
    One removed; the other is still visible in detections.
    Weight drop matches Maggi_10.

    Must NOT become unresolved merely because Maggi_10 remains visible.
    Resolution: Maggi_10 (weight evidence alone).
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10", 2))   # qty = 2

    result = resolver.resolve_remove(
        detections=[_det("Maggi_10", 0.9)],   # one unit still in frame
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1
    assert result.yolo_class == "Maggi_10"


def test_remove_case_b_qty1_gone_from_frame_resolves():
    """
    CASE B (architecture review):
    Cart: Maggi_10 × 1.
    Removed; no longer visible in detections.
    Weight drop matches Maggi_10.

    Resolution: Maggi_10.
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10"))

    result = resolver.resolve_remove(
        detections=[],    # item gone from frame
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1


def test_remove_case_c_ambiguous_weights_unresolved():
    """
    CASE C (architecture review):
    Product A: 20 g.  Product B: 22 g.
    Measured removal: 21 g (within 15 % of both).
    No additional reliable evidence.

    Result: UNRESOLVED / ambiguous.
    """
    product_a = _make_product(10, "ProductA", expected_weight=20.0)
    product_b = _make_product(11, "ProductB", expected_weight=22.0)
    resolver  = _resolver(product_a, product_b)
    state     = _cart((10, "ProductA"), (11, "ProductB"))

    # Verify 21 g is within tolerance for both:
    # ProductA: [17.0, 23.0]   21 ∈ ✓
    # ProductB: [18.7, 25.3]   21 ∈ ✓
    result = resolver.resolve_remove(
        detections=[],
        weight_change=-21.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert "ambiguous" in result.reason


def test_remove_case_d_no_weight_match_unresolved():
    """
    CASE D (architecture review):
    No product in CartState has expected weight compatible with the drop.

    Result: UNRESOLVED.
    """
    resolver = _resolver(MAGGI_10)   # expected 75 g
    state    = _cart((1, "Maggi_10"))

    result = resolver.resolve_remove(
        detections=[],
        weight_change=-500.0,   # no product matches
        cart_state=state,
    )

    assert result.resolved is False
    assert "matches" in result.reason


def test_remove_case_e_duplicate_quantity_not_duplicate_candidates():
    """
    CASE E (architecture review):
    Cart: Maggi_10 × 3 (one CartItem, quantity=3).
    The resolver must NOT treat quantity=3 as three separate candidates.

    Result: RESOLVED with one Maggi_10 (single CartItem, weight matches).
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10", 3))

    result = resolver.resolve_remove(
        detections=[],
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1
    # CartState snapshot has exactly one CartItem (quantity=3), so one candidate.


# ===========================================================================
# 12. Duplicate product quantity handled correctly
# ===========================================================================

def test_remove_duplicate_quantity_resolves():
    """
    Add same product 3 times (quantity = 3).
    One removed (weight_change = expected_weight).
    Must resolve correctly.
    """
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10", 3))   # qty = 3

    result = resolver.resolve_remove(
        detections=[_det("Maggi_10", 0.9)],   # two still visible
        weight_change=-75.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 1


def test_add_duplicate_same_class_does_not_create_multiple_candidates():
    """
    Sending the same Detection twice in one call must not count
    the same product twice as a candidate.
    """
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[
            _det("Maggi_10", 0.9),
            _det("Maggi_10", 0.88),   # same class, second detection
        ],
        weight_change=75.0,
        cart_state=state,
    )

    # Still exactly one distinct candidate → RESOLVED.
    assert result.resolved is True
    assert result.product_id == 1


# ===========================================================================
# 13. Resolver does NOT mutate CartState
# ===========================================================================

def test_resolve_add_does_not_mutate_cart_state():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    snapshot_before = state.snapshot()

    resolver.resolve_add(
        detections=[_det("Maggi_10", 0.9)],
        weight_change=75.0,
        cart_state=state,
    )

    assert state.snapshot() == snapshot_before


def test_resolve_remove_does_not_mutate_cart_state():
    resolver = _resolver(MAGGI_10)
    state    = _cart((1, "Maggi_10"))

    snapshot_before = state.snapshot()

    resolver.resolve_remove(
        detections=[],
        weight_change=-75.0,
        cart_state=state,
    )

    # CartState must be identical after resolution.
    after = state.snapshot()
    assert len(after) == 1
    assert after[0].product_id == snapshot_before[0].product_id
    assert after[0].quantity   == snapshot_before[0].quantity


# ===========================================================================
# 14. Unresolved result contains a useful reason
# ===========================================================================

def test_unresolved_reason_is_non_empty_string():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    cases = [
        # unknown class
        resolver.resolve_add([_det("Ghost", 0.9)], 75.0, state),
        # low confidence
        resolver.resolve_add([_det("Maggi_10", 0.1)], 75.0, state),
        # weight mismatch
        resolver.resolve_add([_det("Maggi_10", 0.9)], 500.0, state),
        # empty cart remove
        resolver.resolve_remove([], -75.0, CartState()),
    ]

    for result in cases:
        assert result.resolved is False
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0, "Unresolved reason must be non-empty"


def test_unresolved_product_id_and_class_are_none():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[_det("Ghost", 0.9)],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.product_id is None
    assert result.yolo_class is None


# ===========================================================================
# Additional edge cases
# ===========================================================================

def test_add_no_detections_returns_unresolved():
    resolver = _resolver(MAGGI_10)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[],
        weight_change=75.0,
        cart_state=state,
    )

    assert result.resolved is False
    assert result.reason  # non-empty


def test_remove_product_not_in_catalog_is_ignored():
    """
    Cart has a product whose yolo_class is not in the resolver's catalog.
    It must be skipped — not crash — and result in UNRESOLVED.
    """
    resolver = _resolver(MAGGI_10)           # catalog only has Maggi_10
    state    = CartState()
    state.add(99, "UnknownClass")            # in cart but not in catalog

    result = resolver.resolve_remove(
        detections=[],
        weight_change=-75.0,
        cart_state=state,
    )

    # The unknown-class item cannot be resolved.
    assert result.resolved is False


def test_custom_threshold_per_class():
    """Per-class threshold override takes precedence over default."""
    # Default threshold = 0.5; override Maggi_10 to require 0.9.
    resolver = _resolver(MAGGI_10, thresholds={"Maggi_10": 0.9})
    state    = CartState()

    # confidence 0.85 < 0.9 → should fail.
    result_fail = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.85)],
        weight_change=75.0,
        cart_state=state,
    )
    assert result_fail.resolved is False

    # confidence 0.9 == 0.9 → should pass.
    result_pass = resolver.resolve_add(
        detections=[_det("Maggi_10", 0.90)],
        weight_change=75.0,
        cart_state=state,
    )
    assert result_pass.resolved is True


def test_resolve_with_multi_product_catalog_picks_correct_one():
    """
    Catalog has three products with distinct weights.
    Detection matches only one; weight change matches only that one.
    """
    resolver = _resolver(MAGGI_10, MAGGI_20, COCA_COLA)
    state    = CartState()

    result = resolver.resolve_add(
        detections=[
            _det("CocaCola", 0.88),
        ],
        weight_change=330.0,
        cart_state=state,
    )

    assert result.resolved is True
    assert result.product_id == 3
    assert result.yolo_class == "CocaCola"


def test_resolution_dataclass_fields():
    """Resolution must expose resolved, product_id, yolo_class, reason."""
    r = Resolution(resolved=True, product_id=1, yolo_class="X", reason="ok")
    assert r.resolved is True
    assert r.product_id == 1
    assert r.yolo_class == "X"
    assert r.reason == "ok"


def test_weight_tolerance_constant_value():
    """Document WEIGHT_TOLERANCE_RATIO value so accidental changes break tests."""
    assert WEIGHT_TOLERANCE_RATIO == 0.15
