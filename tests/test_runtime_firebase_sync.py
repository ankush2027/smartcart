"""
Tests for runtime Firebase synchronization in CartSession.

Covers (per task specification PART 6, 7, 9, 10, 11, 12):

    Runtime synchronization
        - Successful ADD triggers Firebase synchronization
        - Successful REMOVE triggers Firebase synchronization
        - Failed SQLite mutation does NOT trigger Firebase synchronization
        - Firebase receives the current bill item state
        - Firebase receives the current session total
        - Firebase receives the corresponding event log

    Totals (PART 7)
        - new session → total = 0
        - ADD product → total increases
        - ADD same product again → quantity and total update
        - ADD different product → total updates
        - REMOVE one quantity → total decreases
        - REMOVE final quantity → bill item removed/deleted
        - multiple ADD/REMOVE operations → Firebase total matches SQLite total
        - complete session → final total remains available

    Completion (PART 10)
        - end_time and total_amount remain synchronized when session completes

    Failure behavior (PART 11)
        - Firebase failures are not silently swallowed
        - Firebase failure after ADD does NOT roll back SQLite state
        - Firebase failure after REMOVE does NOT roll back SQLite state

Design principles:
    - No real Firebase project required.
    - No service-account JSON required.
    - No environment variables required.
    - No internet access required.
    - firebase_admin is never imported.
    - Injected _FakeReference used for all Firebase assertions.
"""

from __future__ import annotations

# pyrefly: ignore [missing-import]
import pytest

from edge.billing.billing import (
    calculate_total,
    get_bill,
    get_session_meta,
)
from edge.cart_session import CartSession
from edge.database import database as _db_module
from edge.database.database import initialize_database
from edge.database.product_repository import add_product, get_product_by_yolo_class
from edge.detection.product_resolver import Detection, ProductInfo, ProductResolver


# ---------------------------------------------------------------------------
# Fake Firebase references
# ---------------------------------------------------------------------------

class _FakeReference:
    """
    Tree-structured fake for firebase_admin.db.Reference.

    Reuses the same structure as test_firebase_sync._FakeReference so
    that CartSession integration tests assert on the same API surface.
    """

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.last_set_data: dict | None = None
        self.last_update_data: dict | None = None
        self.last_push_data: dict | None = None
        self.deleted: bool = False
        self._children: dict[str, _FakeReference] = {}
        # Track all update() calls in order (session total tracking)
        self._update_calls: list[dict] = []

    def child(self, subpath: str) -> _FakeReference:
        if subpath not in self._children:
            self._children[subpath] = _FakeReference(path=subpath)
        return self._children[subpath]

    def set(self, data: dict) -> None:
        self.last_set_data = data

    def update(self, data: dict) -> None:
        self.last_update_data = data
        self._update_calls.append(data)

    def push(self, data: dict) -> None:
        self.last_push_data = data

    def delete(self) -> None:
        self.deleted = True

    def node(self, subpath: str) -> _FakeReference | None:
        return self._children.get(subpath)


class _ErrorReference:
    """Fake reference that always raises on write operations."""

    def child(self, subpath: str) -> _ErrorReference:
        return self

    def set(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated error")

    def update(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated error")

    def push(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated error")

    def delete(self) -> None:
        raise OSError("Firebase write failed: simulated error")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite database in a temp directory."""
    temp_db = tmp_path / "test_sync_rt.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", temp_db)
    initialize_database()
    return temp_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_product(
    name: str = "Maggi",
    barcode: str = "BC001",
    price: float = 10.0,
    expected_weight: float = 75.0,
    yolo_class: str = "Maggi_10",
) -> ProductInfo:
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


def _make_session(
    *product_infos: ProductInfo,
    firebase_ref=None,
    cart_id: str | None = None,
) -> CartSession:
    resolver = ProductResolver(list(product_infos))
    return CartSession(resolver, cart_id=cart_id, firebase_ref=firebase_ref)


# ---------------------------------------------------------------------------
# No Firebase when firebase_ref is None (backward-compat guard)
# ---------------------------------------------------------------------------

class TestNoFirebaseWhenRefIsNone:
    """CartSession with no firebase_ref must still function correctly."""

    def test_add_succeeds_without_firebase_ref(self):
        maggi = _seed_product()
        session = _make_session(maggi, firebase_ref=None)
        result = session.process_add([_det("Maggi_10")], weight_change=75.0)
        assert result.success is True

    def test_remove_succeeds_without_firebase_ref(self):
        maggi = _seed_product()
        session = _make_session(maggi, firebase_ref=None)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        result = session.process_remove([], weight_change=-75.0)
        assert result.success is True

    def test_complete_succeeds_without_firebase_ref(self):
        maggi = _seed_product()
        session = _make_session(maggi, firebase_ref=None)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        total = session.complete()
        assert total == 10.0


# ---------------------------------------------------------------------------
# ADD triggers Firebase synchronization
# ---------------------------------------------------------------------------

class TestAddTriggersFirebaseSync:

    def test_add_syncs_bill_item_to_firebase(self):
        """A successful ADD must sync the bill item to Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)

        path = f"sessions/{session.session_id}/bill_items/{maggi.product_id}"
        node = root.node(path)
        assert node is not None, f"Expected bill_item node at {path}"
        assert node.last_set_data is not None, "set() must be called for bill item"
        assert node.last_set_data["quantity"] == 1
        assert node.last_set_data["price"] == 10.0
        assert node.last_set_data["total"] == 10.0
        assert node.last_set_data["product_name"] == "Maggi"

    def test_add_syncs_session_total_to_firebase(self):
        """A successful ADD must sync session total_amount to Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)

        session_path = f"sessions/{session.session_id}"
        node = root.node(session_path)
        assert node is not None, "Session node must be updated"
        assert node.last_update_data["total_amount"] == 10.0

    def test_add_syncs_event_log_to_firebase(self):
        """A successful ADD must append to the Firebase event log."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)

        log_path = f"sessions/{session.session_id}/event_log"
        node = root.node(log_path)
        assert node is not None, "event_log node must be created"
        assert node.last_push_data is not None, "push() must be called for event log"
        assert node.last_push_data["event_type"] == "ADD"
        assert node.last_push_data["product_id"] == maggi.product_id
        assert "timestamp" in node.last_push_data

    def test_failed_add_does_not_trigger_firebase_sync(self):
        """
        A failed ADD (product not in DB) must not trigger Firebase sync.
        The Firebase bill item node must not be created.
        """
        ghost = ProductInfo(
            product_id=9999,
            yolo_class="Ghost",
            name="Ghost Product",
            price=0.0,
            expected_weight=75.0,
        )
        root = _FakeReference()
        session = _make_session(ghost, firebase_ref=root)

        result = session.process_add([_det("Ghost")], weight_change=75.0)

        assert result.success is False
        # Firebase must NOT have been touched
        path = f"sessions/{session.session_id}/bill_items/9999"
        assert root.node(path) is None, "Firebase must not be touched on billing failure"

    def test_unresolved_add_does_not_trigger_firebase_sync(self):
        """An unresolved event must not touch Firebase at all."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        result = session.process_add(
            [_det("UnknownProduct")], weight_change=75.0
        )

        assert result.success is False
        assert result.resolved is False
        session_path = f"sessions/{session.session_id}"
        # No session node should have been created since nothing changed
        node = root.node(session_path)
        assert node is None or node.last_update_data is None, (
            "Firebase session must not be synced on unresolved event"
        )

    def test_second_add_updates_quantity_in_firebase(self):
        """Second ADD of same product must update quantity and total in Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)

        path = f"sessions/{session.session_id}/bill_items/{maggi.product_id}"
        node = root.node(path)
        assert node.last_set_data["quantity"] == 2
        assert node.last_set_data["total"] == 20.0


# ---------------------------------------------------------------------------
# REMOVE triggers Firebase synchronization
# ---------------------------------------------------------------------------

class TestRemoveTriggersFirebaseSync:

    def test_remove_syncs_updated_bill_item(self):
        """
        REMOVE when quantity > 1 must sync the updated bill item (not delete).
        """
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        # Add twice, then remove once → quantity should go from 2 to 1
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        path = f"sessions/{session.session_id}/bill_items/{maggi.product_id}"
        node = root.node(path)
        # Last set_data should be quantity=1 after the remove
        assert node.last_set_data["quantity"] == 1
        assert node.last_set_data["total"] == 10.0

    def test_remove_final_unit_deletes_bill_item_in_firebase(self):
        """
        REMOVE when quantity == 1 must call delete() on the Firebase bill item.
        """
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        path = f"sessions/{session.session_id}/bill_items/{maggi.product_id}"
        node = root.node(path)
        assert node is not None, "Node must be navigated to for deletion"
        assert node.deleted is True, "delete() must be called when quantity reaches 0"

    def test_remove_syncs_session_total(self):
        """REMOVE must update session total_amount in Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        session_path = f"sessions/{session.session_id}"
        node = root.node(session_path)
        assert node.last_update_data["total_amount"] == 10.0

    def test_remove_syncs_event_log(self):
        """REMOVE must append a REMOVE entry to Firebase event_log."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        log_path = f"sessions/{session.session_id}/event_log"
        node = root.node(log_path)
        assert node.last_push_data["event_type"] == "REMOVE"
        assert node.last_push_data["product_id"] == maggi.product_id

    def test_failed_remove_does_not_trigger_firebase_sync(self):
        """
        A failed REMOVE (billing fails) must not trigger Firebase sync.
        CartState is rolled back; Firebase must remain unchanged from
        the state after the preceding successful ADD.
        """
        maggi = _seed_product()
        root = _FakeReference()
        resolver = ProductResolver([maggi])

        # session_a adds the product to billing.
        session_a = CartSession(resolver, firebase_ref=root)
        session_a.process_add([_det("Maggi_10")], weight_change=75.0)

        # session_b has a different billing session → remove_item will fail.
        root_b = _FakeReference()
        session_b = CartSession(resolver, firebase_ref=root_b)
        session_b.cart_state.add(maggi.product_id, maggi.yolo_class)

        result = session_b.process_remove([], weight_change=-75.0)

        assert result.success is False
        # Firebase for session_b must have no bill items (no successful add)
        path = f"sessions/{session_b.session_id}/bill_items/{maggi.product_id}"
        assert root_b.node(path) is None, (
            "Firebase must not be touched on a failed REMOVE"
        )

    def test_unresolved_remove_does_not_trigger_firebase_sync(self):
        """Unresolved REMOVE must not touch Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)
        # Cart is empty — resolver returns UNRESOLVED

        result = session.process_remove([], weight_change=-75.0)

        assert result.success is False
        session_path = f"sessions/{session.session_id}"
        node = root.node(session_path)
        assert node is None or node.last_update_data is None, (
            "Firebase must not be synced on unresolved remove"
        )


# ---------------------------------------------------------------------------
# Totals: SQLite total == Firebase synchronized total (PART 7)
# ---------------------------------------------------------------------------

class TestTotalsConsistency:
    """
    Verify that SQLite total always matches the Firebase-synced total
    after sequences of ADD and REMOVE operations.
    """

    def _sqlite_total(self, session: CartSession) -> float:
        """Re-read the total from SQLite."""
        return calculate_total(session.session_id)

    def _firebase_total(self, root: _FakeReference, session: CartSession) -> float | None:
        """Read the latest total_amount from Firebase session update."""
        node = root.node(f"sessions/{session.session_id}")
        if node is None or node.last_update_data is None:
            return None
        return node.last_update_data.get("total_amount")

    def test_new_session_total_is_zero(self):
        """A new session with no items must have total_amount=0 in SQLite."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)
        meta = get_session_meta(session.session_id)
        assert meta["total_amount"] == 0

    def test_add_increases_total(self):
        maggi = _seed_product(price=10.0)
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)

        sqlite_total = self._sqlite_total(session)
        fb_total = self._firebase_total(root, session)
        assert sqlite_total == 10.0
        assert fb_total == sqlite_total

    def test_add_same_product_twice_updates_total(self):
        maggi = _seed_product(price=10.0)
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)

        assert self._sqlite_total(session) == 20.0
        assert self._firebase_total(root, session) == 20.0

    def test_add_different_products_updates_total(self):
        maggi = _seed_product(name="Maggi", barcode="BC001", price=10.0,
                              expected_weight=75.0, yolo_class="Maggi_10")
        bru = _seed_product(name="Bru", barcode="BC002", price=20.0,
                            expected_weight=50.0, yolo_class="Bru_20")
        root = _FakeReference()
        resolver = ProductResolver([maggi, bru])
        session = CartSession(resolver, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Bru_20")], weight_change=50.0)

        assert self._sqlite_total(session) == 30.0
        assert self._firebase_total(root, session) == 30.0

    def test_remove_decreases_total(self):
        maggi = _seed_product(price=10.0)
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        assert self._sqlite_total(session) == 10.0
        assert self._firebase_total(root, session) == 10.0

    def test_remove_final_unit_brings_total_to_zero(self):
        maggi = _seed_product(price=10.0)
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)

        assert self._sqlite_total(session) == 0.0
        assert self._firebase_total(root, session) == 0.0

    def test_multiple_operations_total_matches_sqlite(self):
        """
        Sequence: ADD Maggi ×3, REMOVE ×1, ADD Bru ×2, REMOVE Bru ×2
        Expected final total: Maggi×2 = 20.0
        """
        maggi = _seed_product(name="Maggi", barcode="BC001", price=10.0,
                              expected_weight=75.0, yolo_class="Maggi_10")
        bru = _seed_product(name="Bru", barcode="BC002", price=20.0,
                            expected_weight=50.0, yolo_class="Bru_20")
        root = _FakeReference()
        resolver = ProductResolver([maggi, bru])
        session = CartSession(resolver, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_remove([], weight_change=-75.0)   # Maggi ×2
        session.process_add([_det("Bru_20")], weight_change=50.0)
        session.process_add([_det("Bru_20")], weight_change=50.0)
        session.process_remove([], weight_change=-50.0)   # Bru ×1
        session.process_remove([], weight_change=-50.0)   # Bru ×0 (deleted)

        expected = 20.0  # 2 × Maggi @10
        assert self._sqlite_total(session) == expected
        assert self._firebase_total(root, session) == expected


# ---------------------------------------------------------------------------
# Session completion (PART 10)
# ---------------------------------------------------------------------------

class TestSessionCompletion:

    def test_complete_syncs_end_time_to_firebase(self):
        """complete() must sync end_time to Firebase."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.complete()

        node = root.node(f"sessions/{session.session_id}")
        assert node is not None
        data = node.last_update_data
        assert data is not None
        assert data["end_time"] is not None, "end_time must be set after complete()"

    def test_complete_syncs_final_total(self):
        """complete() must sync the correct final total to Firebase."""
        maggi = _seed_product(price=10.0)
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)

        session.process_add([_det("Maggi_10")], weight_change=75.0)
        session.process_add([_det("Maggi_10")], weight_change=75.0)
        final_total = session.complete()

        node = root.node(f"sessions/{session.session_id}")
        assert node.last_update_data["total_amount"] == final_total
        assert final_total == 20.0

    def test_complete_end_time_is_not_none(self):
        """After complete(), end_time in SQLite must not be None."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)
        session.complete()

        meta = get_session_meta(session.session_id)
        assert meta["end_time"] is not None

    def test_completed_session_firebase_end_time_not_none(self):
        """The Firebase-synced end_time must not be None after complete()."""
        maggi = _seed_product()
        root = _FakeReference()
        session = _make_session(maggi, firebase_ref=root)
        session.complete()

        node = root.node(f"sessions/{session.session_id}")
        assert node.last_update_data["end_time"] is not None


# ---------------------------------------------------------------------------
# Failure behavior (PART 11)
# ---------------------------------------------------------------------------

class TestFirebaseFailureBehavior:

    def test_add_firebase_failure_raises_exception(self):
        """Firebase failure during ADD sync must raise, not be silently swallowed."""
        maggi = _seed_product()
        err_ref = _ErrorReference()
        session = _make_session(maggi, firebase_ref=err_ref)

        with pytest.raises(OSError, match="Firebase write failed"):
            session.process_add([_det("Maggi_10")], weight_change=75.0)

    def test_add_firebase_failure_does_not_rollback_sqlite(self):
        """
        Firebase failure after ADD must NOT roll back the SQLite billing state.
        The product is in the cart and the bill must still reflect the add.
        """
        maggi = _seed_product()
        err_ref = _ErrorReference()
        session = _make_session(maggi, firebase_ref=err_ref)

        try:
            session.process_add([_det("Maggi_10")], weight_change=75.0)
        except OSError:
            pass  # expected

        # SQLite billing must still reflect the add
        bill = get_bill(session.session_id)
        assert len(bill) == 1, (
            "SQLite bill must still have the item despite Firebase failure"
        )
        assert bill[0][2] == 1  # quantity
        assert bill[0][4] == 10.0  # total

    def test_remove_firebase_failure_raises_exception(self):
        """Firebase failure during REMOVE sync must raise, not be swallowed."""
        maggi = _seed_product()
        # Use a working ref for the add, then switch to error ref for remove.
        good_ref = _FakeReference()
        session = _make_session(maggi, firebase_ref=good_ref)
        session.process_add([_det("Maggi_10")], weight_change=75.0)

        # Replace the Firebase ref with one that fails
        session._firebase_ref = _ErrorReference()

        with pytest.raises(OSError, match="Firebase write failed"):
            session.process_remove([], weight_change=-75.0)

    def test_remove_firebase_failure_does_not_rollback_sqlite(self):
        """
        Firebase failure after REMOVE must NOT roll back the SQLite billing state.
        The item has already been removed from the bill.
        """
        maggi = _seed_product()
        good_ref = _FakeReference()
        session = _make_session(maggi, firebase_ref=good_ref)
        session.process_add([_det("Maggi_10")], weight_change=75.0)

        session._firebase_ref = _ErrorReference()

        try:
            session.process_remove([], weight_change=-75.0)
        except OSError:
            pass  # expected

        # SQLite bill must reflect the remove despite Firebase failure
        bill = get_bill(session.session_id)
        assert len(bill) == 0, (
            "SQLite bill must be empty after REMOVE despite Firebase failure"
        )

    def test_sqlite_success_is_independent_of_firebase_success(self):
        """
        SQLite success ≠ Firebase success.  A failing Firebase ref must not
        prevent billing from being recorded in SQLite.

        This verifies the documented SQLite-authoritative architecture rule.
        """
        maggi = _seed_product()
        err_ref = _ErrorReference()
        session = _make_session(maggi, firebase_ref=err_ref)

        # Two ADDs — both will fail Firebase but succeed in SQLite
        for _ in range(2):
            try:
                session.process_add([_det("Maggi_10")], weight_change=75.0)
            except OSError:
                pass

        bill = get_bill(session.session_id)
        assert len(bill) == 1
        assert bill[0][2] == 2   # quantity = 2
        assert bill[0][4] == 20.0  # total = 2 × 10.0
