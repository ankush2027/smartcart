"""
Tests for edge.detection.cart_state — CartState and CartItem.

Covers:
    1.  test_add_one_item               — single add creates quantity=1
    2.  test_add_same_item_twice        — second add increments quantity, no duplicate
    3.  test_remove_existing            — add then remove → item gone from snapshot
    4.  test_remove_one_from_quantity   — add twice, remove once → quantity=1
    5.  test_remove_nonexistent         — remove from empty → False, no exception
    6.  test_snapshot_is_independent    — mutating snapshot doesn't change CartState
    7.  test_get_by_class_found         — retrieve by yolo_class
    8.  test_get_by_class_missing       — class not present → None
    9.  test_multiple_products          — three products are independently maintained
    10. test_quantity_removal_to_empty  — add 3×, remove 3× → empty state
"""

# pyrefly: ignore [missing-import]
import pytest

from edge.detection.cart_state import CartItem, CartState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(*args) -> CartState:
    """Build a CartState from (product_id, yolo_class) tuples."""
    state = CartState()
    for product_id, yolo_class in args:
        state.add(product_id, yolo_class)
    return state


# ---------------------------------------------------------------------------
# 1. test_add_one_item
# ---------------------------------------------------------------------------

def test_add_one_item():
    state = CartState()
    state.add(1, "bottle")

    snap = state.snapshot()

    assert len(snap) == 1
    assert snap[0].product_id == 1
    assert snap[0].yolo_class == "bottle"
    assert snap[0].quantity == 1


# ---------------------------------------------------------------------------
# 2. test_add_same_item_twice
# ---------------------------------------------------------------------------

def test_add_same_item_twice():
    state = CartState()
    state.add(1, "Maggi_10")
    state.add(1, "Maggi_10")

    snap = state.snapshot()

    # Exactly one CartItem record — no duplicates.
    assert len(snap) == 1
    assert snap[0].product_id == 1
    assert snap[0].yolo_class == "Maggi_10"
    assert snap[0].quantity == 2


# ---------------------------------------------------------------------------
# 3. test_remove_existing
# ---------------------------------------------------------------------------

def test_remove_existing():
    state = CartState()
    state.add(42, "CocaCola")

    result = state.remove(42)

    assert result is True
    snap = state.snapshot()
    assert len(snap) == 0


# ---------------------------------------------------------------------------
# 4. test_remove_one_from_quantity
# ---------------------------------------------------------------------------

def test_remove_one_from_quantity():
    state = CartState()
    state.add(5, "Lays")
    state.add(5, "Lays")

    result = state.remove(5)

    assert result is True
    snap = state.snapshot()
    assert len(snap) == 1
    assert snap[0].product_id == 5
    assert snap[0].quantity == 1


# ---------------------------------------------------------------------------
# 5. test_remove_nonexistent
# ---------------------------------------------------------------------------

def test_remove_nonexistent():
    state = CartState()

    result = state.remove(99)

    assert result is False
    # State must remain empty.
    assert len(state.snapshot()) == 0


# ---------------------------------------------------------------------------
# 6. test_snapshot_is_independent
# ---------------------------------------------------------------------------

def test_snapshot_is_independent():
    state = CartState()
    state.add(7, "Colgate_150g")

    snap = state.snapshot()

    # Mutate the snapshot's item directly.
    snap[0].quantity = 999
    snap[0].yolo_class = "CORRUPTED"

    # Clear the list itself too.
    snap.clear()

    # Internal CartState must be untouched.
    fresh_snap = state.snapshot()
    assert len(fresh_snap) == 1
    assert fresh_snap[0].quantity == 1
    assert fresh_snap[0].yolo_class == "Colgate_150g"


# ---------------------------------------------------------------------------
# 7. test_get_by_class_found
# ---------------------------------------------------------------------------

def test_get_by_class_found():
    state = CartState()
    state.add(3, "Maggi_10")

    item = state.get_by_class("Maggi_10")

    assert item is not None
    assert isinstance(item, CartItem)
    assert item.product_id == 3
    assert item.yolo_class == "Maggi_10"
    assert item.quantity == 1


# ---------------------------------------------------------------------------
# 8. test_get_by_class_missing
# ---------------------------------------------------------------------------

def test_get_by_class_missing():
    state = CartState()
    state.add(3, "Maggi_10")

    result = state.get_by_class("GhostProduct")

    assert result is None


# ---------------------------------------------------------------------------
# 9. test_multiple_products
# ---------------------------------------------------------------------------

def test_multiple_products():
    state = CartState()
    state.add(1, "Maggi_10")
    state.add(2, "Maggi_20")
    state.add(3, "Colgate_150g")

    snap = state.snapshot()
    assert len(snap) == 3

    # Build a quick lookup by product_id for assertions.
    by_id = {item.product_id: item for item in snap}

    assert by_id[1].yolo_class == "Maggi_10"
    assert by_id[1].quantity == 1

    assert by_id[2].yolo_class == "Maggi_20"
    assert by_id[2].quantity == 1

    assert by_id[3].yolo_class == "Colgate_150g"
    assert by_id[3].quantity == 1

    # Removing one product must not affect the others.
    state.remove(2)
    snap2 = state.snapshot()
    assert len(snap2) == 2

    ids_after = {item.product_id for item in snap2}
    assert 1 in ids_after
    assert 3 in ids_after
    assert 2 not in ids_after


# ---------------------------------------------------------------------------
# 10. test_quantity_removal_to_empty
# ---------------------------------------------------------------------------

def test_quantity_removal_to_empty():
    state = CartState()
    state.add(9, "BigCola")
    state.add(9, "BigCola")
    state.add(9, "BigCola")

    assert state.snapshot()[0].quantity == 3

    r1 = state.remove(9)
    r2 = state.remove(9)
    r3 = state.remove(9)

    assert r1 is True
    assert r2 is True
    assert r3 is True

    # Cart must be completely empty.
    assert len(state.snapshot()) == 0

    # A further remove must return False gracefully.
    r4 = state.remove(9)
    assert r4 is False


# ---------------------------------------------------------------------------
# Extra edge-case: get_by_class returns live item (not copy)
# (Documents that get_by_class returns a reference to the internal item.)
# ---------------------------------------------------------------------------

def test_get_by_class_returns_live_reference():
    """
    get_by_class intentionally returns the live CartItem so that callers
    can read it cheaply.  Callers who need an isolated copy should use
    snapshot().  This test documents that behaviour explicitly.
    """
    state = CartState()
    state.add(1, "Biscuit")

    item = state.get_by_class("Biscuit")
    assert item is not None

    # Mutating the returned reference affects internal state.
    # (This is by design — snapshot() is the safe copy mechanism.)
    item.quantity = 50

    retrieved_again = state.get_by_class("Biscuit")
    assert retrieved_again.quantity == 50
