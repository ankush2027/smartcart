"""
Tests for edge.cart_session — CartSession.

All tests use a temporary SQLite database (via pytest's tmp_path + monkeypatch)
so no data is written to the real data/smartcart.db.

Products are seeded into the temp DB before each test so that billing can look
up prices.  The product_id in ProductInfo is obtained from the DB after
insertion to guarantee it matches.

No YOLO model, no camera, no Raspberry Pi, no HX711.

Required cases (per task spec):
 1.  Resolved ADD updates CartState.
 2.  Resolved ADD updates billing.
 3.  Resolved REMOVE updates CartState.
 4.  Resolved REMOVE updates billing.
 5.  Unresolved event changes neither CartState nor billing.
 6.  REMOVE of nonexistent cart item is rejected safely.
 7.  Multiple ADD events increase quantity correctly.
 8.  ADD followed by REMOVE returns quantity correctly.
 9.  No duplicate billing operation for one event.
10.  CartSession does not use global mutable state.

Additional cases:
11.  Billing failure on ADD leaves CartState unchanged.
12.  Billing failure on REMOVE rolls back CartState.
13.  complete() returns correct total after ADDs.
14.  SessionResult fields are correct on success and failure.
15.  Resolved ADD with bad product_id (not in DB) fails cleanly.
"""

# pyrefly: ignore [missing-import]
import pytest

from edge.billing.billing import get_bill, get_session_log
from edge.cart_session import CartSession, SessionResult
from edge.database import database as _db_module
from edge.database.database import initialize_database
from edge.database.product_repository import add_product, get_product_by_yolo_class
from edge.detection.product_resolver import Detection, ProductInfo, ProductResolver


# ---------------------------------------------------------------------------
# Fixture: isolated SQLite database
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Each test gets a fresh SQLite database in a temp directory.
    DATABASE_PATH is monkeypatched at the module level so that billing,
    repository, and database functions all use the same temp DB.
    """
    temp_db = tmp_path / "test_smartcart.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", temp_db)
    initialize_database()
    return temp_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_product(
    name: str,
    barcode: str,
    price: float,
    expected_weight: float,
    yolo_class: str,
) -> ProductInfo:
    """
    Insert a product into the temp DB and return a ProductInfo whose
    product_id matches the actual auto-incremented DB row.
    """
    add_product(name, barcode, price, expected_weight, yolo_class=yolo_class)
    row = get_product_by_yolo_class(yolo_class)
    return ProductInfo(
        product_id=row[0],
        yolo_class=yolo_class,
        name=name,
        price=price,
        expected_weight=expected_weight,
    )


def _det(yolo_class: str, confidence: float = 0.9) -> Detection:
    return Detection(yolo_class=yolo_class, confidence=confidence)


def _make_session(*product_infos: ProductInfo) -> CartSession:
    """Build a CartSession with a resolver loaded from the supplied catalog."""
    resolver = ProductResolver(list(product_infos))
    return CartSession(resolver)


# ---------------------------------------------------------------------------
# 1. Resolved ADD updates CartState
# ---------------------------------------------------------------------------

def test_add_updates_cart_state():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    result = session.process_add(
        detections=[_det("Maggi_10", 0.92)],
        weight_change=75.0,
    )

    assert result.success is True
    assert result.resolved is True
    assert result.product_id == maggi.product_id
    assert result.yolo_class == "Maggi_10"

    items = session.cart_state.snapshot()
    assert len(items) == 1
    assert items[0].product_id == maggi.product_id
    assert items[0].quantity == 1


# ---------------------------------------------------------------------------
# 2. Resolved ADD updates billing
# ---------------------------------------------------------------------------

def test_add_updates_billing():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(
        detections=[_det("Maggi_10", 0.92)],
        weight_change=75.0,
    )

    bill = session.get_bill()
    assert len(bill) == 1
    # bill row: (product_id, name, quantity, price, total)
    assert bill[0][0] == maggi.product_id
    assert bill[0][2] == 1        # quantity
    assert bill[0][4] == 10.0    # total = 1 × 10.0


# ---------------------------------------------------------------------------
# 3. Resolved REMOVE updates CartState
# ---------------------------------------------------------------------------

def test_remove_updates_cart_state():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    result = session.process_remove(
        detections=[],
        weight_change=-75.0,
    )

    assert result.success is True
    assert result.product_id == maggi.product_id

    items = session.cart_state.snapshot()
    assert len(items) == 0


# ---------------------------------------------------------------------------
# 4. Resolved REMOVE updates billing
# ---------------------------------------------------------------------------

def test_remove_updates_billing():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_remove(detections=[], weight_change=-75.0)

    bill = session.get_bill()
    assert len(bill) == 0    # item fully removed from bill


# ---------------------------------------------------------------------------
# 5. Unresolved event changes neither CartState nor billing
# ---------------------------------------------------------------------------

def test_unresolved_event_changes_nothing():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    # Unknown class → resolver returns UNRESOLVED
    result = session.process_add(
        detections=[_det("GhostProduct", 0.95)],
        weight_change=75.0,
    )

    assert result.success is False
    assert result.resolved is False
    assert result.product_id is None
    assert result.yolo_class is None
    assert len(result.reason) > 0

    # CartState untouched
    assert len(session.cart_state.snapshot()) == 0

    # Billing untouched
    assert len(session.get_bill()) == 0


def test_unresolved_remove_changes_nothing():
    """REMOVE on empty cart → resolver UNRESOLVED → nothing modified."""
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)
    # CartState is empty; resolver will return UNRESOLVED.

    result = session.process_remove(
        detections=[],
        weight_change=-75.0,
    )

    assert result.success is False
    assert result.resolved is False
    assert len(session.cart_state.snapshot()) == 0
    assert len(session.get_bill()) == 0


# ---------------------------------------------------------------------------
# 6. REMOVE of nonexistent cart item is rejected safely
# ---------------------------------------------------------------------------

def test_remove_of_nonexistent_item_is_safe():
    """
    When CartState is empty, resolve_remove returns UNRESOLVED (cart empty).
    CartSession must surface this cleanly without crashing or corrupting billing.
    """
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    result = session.process_remove(
        detections=[],
        weight_change=-75.0,
    )

    assert result.success is False
    assert "empty" in result.reason.lower()
    assert len(session.cart_state.snapshot()) == 0
    assert len(session.get_bill()) == 0


# ---------------------------------------------------------------------------
# 7. Multiple ADD events increase quantity correctly
# ---------------------------------------------------------------------------

def test_multiple_adds_increase_quantity():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    items = session.cart_state.snapshot()
    assert len(items) == 1
    assert items[0].quantity == 3

    bill = session.get_bill()
    assert bill[0][2] == 3       # quantity in billing
    assert bill[0][4] == 30.0   # total = 3 × 10.0


# ---------------------------------------------------------------------------
# 8. ADD followed by REMOVE returns quantity correctly
# ---------------------------------------------------------------------------

def test_add_twice_remove_once_leaves_quantity_one():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    result = session.process_remove(
        detections=[_det("Maggi_10", 0.9)],
        weight_change=-75.0,
    )

    assert result.success is True

    items = session.cart_state.snapshot()
    assert items[0].quantity == 1

    bill = session.get_bill()
    assert bill[0][2] == 1
    assert bill[0][4] == 10.0


# ---------------------------------------------------------------------------
# 9. No duplicate billing operation for one event
# ---------------------------------------------------------------------------

def test_single_add_creates_one_billing_entry():
    """One process_add call must create exactly one billing line with qty=1."""
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    bill = session.get_bill()
    assert len(bill) == 1
    assert bill[0][2] == 1    # quantity must be exactly 1, not 2


def test_single_remove_decrements_billing_once():
    """One process_remove call must decrement billing exactly once."""
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    session.process_remove(detections=[], weight_change=-75.0)

    bill = session.get_bill()
    assert bill[0][2] == 1    # quantity must be 1, not 0


# ---------------------------------------------------------------------------
# 10. CartSession does not use global mutable state
# ---------------------------------------------------------------------------

def test_two_sessions_are_independent():
    """
    Two CartSession instances must have independent CartStates and different
    billing session_ids.  Modifying one must not affect the other.
    """
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    resolver = ProductResolver([maggi])

    session_a = CartSession(resolver)
    session_b = CartSession(resolver)

    # session_a and session_b get separate billing sessions.
    assert session_a.session_id != session_b.session_id

    # Add to session_a only.
    session_a.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    # session_b CartState must remain empty.
    assert len(session_b.cart_state.snapshot()) == 0

    # session_b billing must remain empty.
    assert len(session_b.get_bill()) == 0

    # session_a CartState has one item.
    assert len(session_a.cart_state.snapshot()) == 1


# ---------------------------------------------------------------------------
# 11. Billing failure on ADD leaves CartState unchanged
# ---------------------------------------------------------------------------

def test_add_billing_failure_leaves_cart_state_unchanged():
    """
    If billing.add_item() returns False (product not in DB), CartState must
    remain unchanged.  This tests the 'billing first' ordering for ADD.
    """
    # Create ProductInfo with a product_id that does NOT exist in the DB.
    ghost = ProductInfo(
        product_id=9999,   # no such row in products table
        yolo_class="Ghost",
        name="Ghost Product",
        price=0.0,
        expected_weight=75.0,
    )
    resolver = ProductResolver([ghost])
    session = CartSession(resolver)

    result = session.process_add(
        detections=[_det("Ghost", 0.95)],
        weight_change=75.0,
    )

    assert result.success is False
    assert result.resolved is True          # resolver found the class
    assert "billing" in result.reason.lower()

    # CartState must NOT have been updated.
    assert len(session.cart_state.snapshot()) == 0


# ---------------------------------------------------------------------------
# 12. Billing failure on REMOVE rolls back CartState
# ---------------------------------------------------------------------------

def test_remove_billing_failure_rolls_back_cart_state():
    """
    If billing.remove_item() returns False (item not in bill for this session),
    CartState must be restored to its state before the REMOVE call.
    """
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    resolver = ProductResolver([maggi])

    # Session A adds the product to both CartState and billing.
    session_a = CartSession(resolver)
    session_a.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    # Session B has the same CartState (manipulated directly for test purposes)
    # but a *different* billing session (so billing.remove_item returns False).
    session_b = CartSession(resolver)
    # Manually add to session_b CartState so resolve_remove can find the item.
    session_b.cart_state.add(maggi.product_id, maggi.yolo_class)
    # session_b billing has NO bill item → remove_item will return False.

    snapshot_before = session_b.cart_state.snapshot()

    result = session_b.process_remove(
        detections=[],
        weight_change=-75.0,
    )

    assert result.success is False
    assert "rolled back" in result.reason

    # CartState must be restored to its pre-REMOVE state.
    snapshot_after = session_b.cart_state.snapshot()
    assert len(snapshot_after) == 1
    assert snapshot_after[0].quantity == snapshot_before[0].quantity


# ---------------------------------------------------------------------------
# 13. complete() returns correct total after ADDs
# ---------------------------------------------------------------------------

def test_complete_returns_correct_total():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    total = session.complete()

    assert total == 20.0    # 2 × ₹10


# ---------------------------------------------------------------------------
# 14. SessionResult fields are correct on success and failure
# ---------------------------------------------------------------------------

def test_session_result_on_success():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    result = session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)

    assert isinstance(result, SessionResult)
    assert result.success is True
    assert result.resolved is True
    assert result.product_id == maggi.product_id
    assert result.yolo_class == "Maggi_10"
    assert result.reason == "ok"


def test_session_result_on_unresolved():
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    result = session.process_add(
        detections=[_det("Ghost", 0.95)],   # not in catalog
        weight_change=75.0,
    )

    assert isinstance(result, SessionResult)
    assert result.success is False
    assert result.resolved is False
    assert result.product_id is None
    assert result.yolo_class is None
    assert len(result.reason) > 0


# ---------------------------------------------------------------------------
# 15. session_log records ADD and REMOVE events
# ---------------------------------------------------------------------------

def test_session_log_records_events():
    """
    billing.session_log should capture one ADD and one REMOVE entry.
    """
    maggi = _seed_product("Maggi ₹10", "BC001", 10.0, 75.0, "Maggi_10")
    session = _make_session(maggi)

    session.process_add(detections=[_det("Maggi_10")], weight_change=75.0)
    session.process_remove(detections=[], weight_change=-75.0)

    log = session.get_session_log()
    event_types = [entry[3] for entry in log]   # entry[3] = event_type column

    assert "ADD" in event_types
    assert "REMOVE" in event_types
