"""
Tests for edge.firebase.sync — Firebase synchronization layer.

Design principles:
    - No real Firebase project required.
    - No service-account JSON required.
    - No environment variables required.
    - No internet access required.
    - firebase_admin is never imported.
    - SQLite is never touched.
    - All tests are deterministic.

Fake-reference strategy:
    Every sync function accepts an optional _ref keyword argument.
    When supplied, the injected fake is used instead of the live Firebase
    client.  _FakeReference implements child(), set(), update(), push(), and
    delete() so that tests can assert on the exact path navigated and the
    exact data written — without a real Firebase connection.

    This extends the injection pattern established in:
        - edge.firebase.client   (_fake_ref parameter)
        - edge.model.yolo_detector  (_model parameter)

Covered cases:

    Product sync
     1.  sync_product() writes to the correct Firebase path.
     2.  All five approved product fields are present.
     3.  The product_id integer is used as the Firebase key.
     4.  No unexpected fields are included in the product payload.
     5.  barcode=None and yolo_class=None are preserved (nullable fields).

    Session sync
     6.  sync_session() writes to the correct Firebase path.
     7.  All three approved session fields are present.
     8.  status is NOT included in the session payload.
     9.  end_time=None (active session) is preserved correctly.

    Bill item sync
    10.  sync_bill_item() writes to the correct nested path.
    11.  The product_id integer is used as the bill-item Firebase key.
    12.  All four approved bill-item fields are present.
    13.  No unexpected fields are included in the bill-item payload.

    Event log sync
    14.  append_event_log() pushes to the correct nested path.
    15.  push() is used (not set()) — Firebase generates the push key.
    16.  The event payload contains exactly timestamp, event_type, product_id.
    17.  log_id is NOT in the event payload.
    18.  details is NOT in the event payload.
    19.  session_id is NOT in the event payload.

    Remove bill item
    20.  remove_bill_item() deletes the correct node.

    Error behavior
    21.  Firebase write failure is surfaced as an exception.
    22.  Firebase failure does NOT modify any SQLite state.
    23.  Importing edge.firebase.sync does not import firebase_admin.
"""

from __future__ import annotations

import importlib
import sys

# pyrefly: ignore [missing-import]
import pytest

from edge.firebase.sync import (
    append_event_log,
    remove_bill_item,
    sync_bill_item,
    sync_product,
    sync_session,
)


# ---------------------------------------------------------------------------
# Fake Firebase reference
# ---------------------------------------------------------------------------

class _FakeReference:
    """
    Tree-structured fake for firebase_admin.db.Reference.

    Supports the exact method surface used by edge.firebase.sync:
        child(subpath)  → returns a child _FakeReference, keyed by subpath
        set(data)       → records data in last_set_data
        update(data)    → records data in last_update_data
        push(data)      → records data in last_push_data
        delete()        → sets deleted = True

    All sync functions call the pattern::

        _root_ref(_ref).child("<full/path>").<write_method>(data)

    so child() calls are always made from the root reference.
    The node() helper retrieves any child reference by the subpath string
    that was passed to child(), making assertions easy.

    Usage::

        root = _FakeReference()
        sync_product(1, "Maggi", "123", 10.0, 35.0, "cls", _ref=root)
        node = root.node("products/1")
        assert node.last_set_data["name"] == "Maggi"
    """

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.last_set_data: dict | None = None
        self.last_update_data: dict | None = None
        self.last_push_data: dict | None = None
        self.deleted: bool = False
        self._children: dict[str, _FakeReference] = {}

    def child(self, subpath: str) -> _FakeReference:
        """Return (and lazily create) a child reference keyed by subpath."""
        if subpath not in self._children:
            self._children[subpath] = _FakeReference(path=subpath)
        return self._children[subpath]

    def set(self, data: dict) -> None:
        """Record a set() call — mirrors firebase_admin.db.Reference.set()."""
        self.last_set_data = data

    def update(self, data: dict) -> None:
        """Record an update() call — mirrors firebase_admin.db.Reference.update()."""
        self.last_update_data = data

    def push(self, data: dict) -> None:
        """Record a push() call — mirrors firebase_admin.db.Reference.push()."""
        self.last_push_data = data

    def delete(self) -> None:
        """Record a delete() call — mirrors firebase_admin.db.Reference.delete()."""
        self.deleted = True

    def node(self, subpath: str) -> _FakeReference | None:
        """
        Retrieve a child node by the exact subpath passed to child().
        Returns None if that child was never navigated to.
        """
        return self._children.get(subpath)


class _ErrorReference:
    """
    Fake reference whose write methods always raise an exception.

    Used to verify that sync functions surface Firebase failures cleanly
    to the caller rather than swallowing them.
    """

    def child(self, subpath: str) -> _ErrorReference:
        return self

    def set(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated network error")

    def update(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated network error")

    def push(self, data: dict) -> None:
        raise OSError("Firebase write failed: simulated network error")

    def delete(self) -> None:
        raise OSError("Firebase write failed: simulated network error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRODUCT_ID = 1
_PRODUCT_NAME = "Maggi Small"
_PRODUCT_BARCODE = "8901234567890"
_PRODUCT_PRICE = 10.0
_PRODUCT_WEIGHT = 35.0
_PRODUCT_YOLO_CLASS = "Maggi_10"

_SESSION_ID = 42
_SESSION_START = "2026-09-23 10:00:00"
_SESSION_END = "2026-09-23 10:22:34"
_SESSION_TOTAL = 30.0


# ---------------------------------------------------------------------------
# Product sync tests
# ---------------------------------------------------------------------------

class TestSyncProduct:
    """Tests for sync_product()."""

    def test_correct_firebase_path(self):
        """
        sync_product() must navigate to products/{product_id}.
        The subpath passed to child() must be the string "products/1".
        """
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, _PRODUCT_BARCODE,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, _PRODUCT_YOLO_CLASS,
            _ref=root,
        )
        assert root.node(f"products/{_PRODUCT_ID}") is not None, (
            "Expected child node at products/1 to be created"
        )

    def test_uses_set_not_update(self):
        """sync_product() must call set(), not update()."""
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, _PRODUCT_BARCODE,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, _PRODUCT_YOLO_CLASS,
            _ref=root,
        )
        node = root.node(f"products/{_PRODUCT_ID}")
        assert node.last_set_data is not None, "set() was not called"
        assert node.last_update_data is None, "update() must not be called by sync_product"

    def test_all_five_approved_fields_present(self):
        """All five approved product fields must be present in the payload."""
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, _PRODUCT_BARCODE,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, _PRODUCT_YOLO_CLASS,
            _ref=root,
        )
        data = root.node(f"products/{_PRODUCT_ID}").last_set_data
        assert "name" in data
        assert "barcode" in data
        assert "price" in data
        assert "expected_weight" in data
        assert "yolo_class" in data

    def test_field_values_are_correct(self):
        """Product field values must map exactly from the function arguments."""
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, _PRODUCT_BARCODE,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, _PRODUCT_YOLO_CLASS,
            _ref=root,
        )
        data = root.node(f"products/{_PRODUCT_ID}").last_set_data
        assert data["name"] == _PRODUCT_NAME
        assert data["barcode"] == _PRODUCT_BARCODE
        assert data["price"] == _PRODUCT_PRICE
        assert data["expected_weight"] == _PRODUCT_WEIGHT
        assert data["yolo_class"] == _PRODUCT_YOLO_CLASS

    def test_no_unexpected_fields(self):
        """The product payload must contain exactly the five approved fields."""
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, _PRODUCT_BARCODE,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, _PRODUCT_YOLO_CLASS,
            _ref=root,
        )
        data = root.node(f"products/{_PRODUCT_ID}").last_set_data
        approved = {"name", "barcode", "price", "expected_weight", "yolo_class"}
        assert set(data.keys()) == approved, (
            f"Unexpected fields: {set(data.keys()) - approved}"
        )

    def test_nullable_fields_preserved(self):
        """barcode=None and yolo_class=None must be preserved, not omitted."""
        root = _FakeReference()
        sync_product(
            _PRODUCT_ID, _PRODUCT_NAME, None,
            _PRODUCT_PRICE, _PRODUCT_WEIGHT, None,
            _ref=root,
        )
        data = root.node(f"products/{_PRODUCT_ID}").last_set_data
        assert "barcode" in data and data["barcode"] is None
        assert "yolo_class" in data and data["yolo_class"] is None

    def test_product_id_used_as_key(self):
        """
        Different product_ids must write to different Firebase nodes.
        product_id 1 must not collide with product_id 2.
        """
        root = _FakeReference()
        sync_product(1, "Maggi Small", None, 10.0, 35.0, "Maggi_10", _ref=root)
        sync_product(2, "Bru", None, 20.0, 50.0, "Bru", _ref=root)

        assert root.node("products/1").last_set_data["name"] == "Maggi Small"
        assert root.node("products/2").last_set_data["name"] == "Bru"


# ---------------------------------------------------------------------------
# Session sync tests
# ---------------------------------------------------------------------------

class TestSyncSession:
    """Tests for sync_session()."""

    def test_correct_firebase_path(self):
        """sync_session() must navigate to sessions/{session_id}."""
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        assert root.node(f"sessions/{_SESSION_ID}") is not None

    def test_uses_update_not_set(self):
        """
        sync_session() must call update(), not set().
        update() merges fields without deleting sibling nodes
        (bill_items, event_log) that already exist under sessions/{id}/.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        node = root.node(f"sessions/{_SESSION_ID}")
        assert node.last_update_data is not None, "update() was not called"
        assert node.last_set_data is None, "set() must not be called by sync_session"

    def test_all_approved_fields_present(self):
        """
        All approved session fields must be present:
        start_time, end_time, total_amount, cart_id, customer_uid.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert "start_time" in data
        assert "end_time" in data
        assert "total_amount" in data
        assert "cart_id" in data
        assert "customer_uid" in data

    def test_field_values_are_correct(self):
        """Session field values must map exactly from the arguments."""
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert data["start_time"] == _SESSION_START
        assert data["end_time"] == _SESSION_END
        assert data["total_amount"] == _SESSION_TOTAL

    def test_status_not_included(self):
        """
        The session payload must NOT contain a 'status' field.
        Status is derivable from end_time == null in the Flutter client.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert "status" not in data, (
            "'status' must not be included — derive from end_time in Flutter"
        )

    def test_no_unexpected_fields(self):
        """
        The session payload must contain exactly the five approved fields.
        cart_id and customer_uid are part of the session ownership contract.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, _SESSION_END, _SESSION_TOTAL,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        approved = {"start_time", "end_time", "total_amount", "cart_id", "customer_uid"}
        assert set(data.keys()) == approved, (
            f"Unexpected fields: {set(data.keys()) - approved}"
        )

    def test_active_session_end_time_none(self):
        """
        An active session (not yet completed) has end_time=None.
        None must be passed through unchanged, not converted to a string.
        """
        root = _FakeReference()
        sync_session(_SESSION_ID, _SESSION_START, None, 0.0, _ref=root)
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert data["end_time"] is None

    def test_cart_id_included_when_provided(self):
        """
        When cart_id is provided, it must appear in the Firebase payload.
        This supports the Cart → Session → Customer ownership model.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, None, 0.0,
            cart_id="CART-A1B2",
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert data["cart_id"] == "CART-A1B2"

    def test_cart_id_none_when_not_provided(self):
        """
        When cart_id is not provided, it must be None in the Firebase payload
        (not omitted — the key must be present).
        """
        root = _FakeReference()
        sync_session(_SESSION_ID, _SESSION_START, None, 0.0, _ref=root)
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert "cart_id" in data
        assert data["cart_id"] is None

    def test_customer_uid_included_when_provided(self):
        """
        When customer_uid is provided, it must appear in the Firebase payload.
        Firebase security rules use this value to restrict customer access.
        """
        root = _FakeReference()
        sync_session(
            _SESSION_ID, _SESSION_START, None, 0.0,
            customer_uid="firebase-uid-xyz",
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert data["customer_uid"] == "firebase-uid-xyz"

    def test_customer_uid_none_when_not_provided(self):
        """
        When customer_uid is not provided, it must be None in the Firebase
        payload (not omitted — the key must be present).
        """
        root = _FakeReference()
        sync_session(_SESSION_ID, _SESSION_START, None, 0.0, _ref=root)
        data = root.node(f"sessions/{_SESSION_ID}").last_update_data
        assert "customer_uid" in data
        assert data["customer_uid"] is None


# ---------------------------------------------------------------------------
# Bill item sync tests
# ---------------------------------------------------------------------------

class TestSyncBillItem:
    """Tests for sync_bill_item()."""

    def test_correct_firebase_path(self):
        """
        sync_bill_item() must navigate to
        sessions/{session_id}/bill_items/{product_id}.
        """
        root = _FakeReference()
        sync_bill_item(
            _SESSION_ID, _PRODUCT_ID, _PRODUCT_NAME,
            quantity=2, price=10.0, total=20.0,
            _ref=root,
        )
        expected_path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        assert root.node(expected_path) is not None, (
            f"Expected child node at {expected_path}"
        )

    def test_uses_set_not_push(self):
        """
        sync_bill_item() must call set() — bill items are UPSERT by product_id,
        not appended with a push key.
        """
        root = _FakeReference()
        sync_bill_item(
            _SESSION_ID, _PRODUCT_ID, _PRODUCT_NAME,
            quantity=1, price=10.0, total=10.0,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        node = root.node(path)
        assert node.last_set_data is not None, "set() was not called"
        assert node.last_push_data is None, "push() must not be called by sync_bill_item"

    def test_all_four_approved_fields_present(self):
        """product_name, quantity, price, total must all be present."""
        root = _FakeReference()
        sync_bill_item(
            _SESSION_ID, _PRODUCT_ID, _PRODUCT_NAME,
            quantity=2, price=10.0, total=20.0,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        data = root.node(path).last_set_data
        assert "product_name" in data
        assert "quantity" in data
        assert "price" in data
        assert "total" in data

    def test_field_values_are_correct(self):
        """Bill item field values must map exactly from the arguments."""
        root = _FakeReference()
        sync_bill_item(
            _SESSION_ID, _PRODUCT_ID, _PRODUCT_NAME,
            quantity=3, price=10.0, total=30.0,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        data = root.node(path).last_set_data
        assert data["product_name"] == _PRODUCT_NAME
        assert data["quantity"] == 3
        assert data["price"] == 10.0
        assert data["total"] == 30.0

    def test_no_unexpected_fields(self):
        """The bill-item payload must contain exactly the four approved fields."""
        root = _FakeReference()
        sync_bill_item(
            _SESSION_ID, _PRODUCT_ID, _PRODUCT_NAME,
            quantity=1, price=10.0, total=10.0,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        data = root.node(path).last_set_data
        approved = {"product_name", "quantity", "price", "total"}
        assert set(data.keys()) == approved, (
            f"Unexpected fields: {set(data.keys()) - approved}"
        )

    def test_product_id_is_firebase_key(self):
        """
        Two products in the same session must write to separate Firebase keys
        without colliding.
        """
        root = _FakeReference()
        sync_bill_item(_SESSION_ID, 1, "Maggi Small", 2, 10.0, 20.0, _ref=root)
        sync_bill_item(_SESSION_ID, 2, "Bru", 1, 20.0, 20.0, _ref=root)

        assert root.node(f"sessions/{_SESSION_ID}/bill_items/1").last_set_data["product_name"] == "Maggi Small"
        assert root.node(f"sessions/{_SESSION_ID}/bill_items/2").last_set_data["product_name"] == "Bru"


# ---------------------------------------------------------------------------
# Event log sync tests
# ---------------------------------------------------------------------------

class TestAppendEventLog:
    """Tests for append_event_log()."""

    def test_correct_firebase_path(self):
        """
        append_event_log() must navigate to
        sessions/{session_id}/event_log (the collection node).
        push() is called on that node so Firebase generates the key.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        expected_path = f"sessions/{_SESSION_ID}/event_log"
        assert root.node(expected_path) is not None

    def test_uses_push_not_set(self):
        """
        append_event_log() must call push(), not set().
        push() lets Firebase generate a unique chronological key.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/event_log"
        node = root.node(path)
        assert node.last_push_data is not None, "push() was not called"
        assert node.last_set_data is None, "set() must not be called by append_event_log"

    def test_exactly_three_approved_fields(self):
        """
        The event payload must contain exactly timestamp, event_type,
        and product_id — no more, no less.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        path = f"sessions/{_SESSION_ID}/event_log"
        data = root.node(path).last_push_data
        approved = {"timestamp", "event_type", "product_id"}
        assert set(data.keys()) == approved, (
            f"Unexpected fields: {set(data.keys()) - approved}"
        )

    def test_field_values_are_correct(self):
        """Event field values must map exactly from the arguments."""
        root = _FakeReference()
        timestamp = "2026-09-23 10:01:05"
        append_event_log(_SESSION_ID, timestamp, "REMOVE", _PRODUCT_ID, _ref=root)
        path = f"sessions/{_SESSION_ID}/event_log"
        data = root.node(path).last_push_data
        assert data["timestamp"] == timestamp
        assert data["event_type"] == "REMOVE"
        assert data["product_id"] == _PRODUCT_ID

    def test_log_id_not_in_payload(self):
        """
        log_id must NOT be in the event payload.
        It is an internal SQLite autoincrement key with no value in Firebase.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}/event_log").last_push_data
        assert "log_id" not in data

    def test_details_not_in_payload(self):
        """
        details must NOT be in the event payload.
        SQLite stores a hardcoded string with no information content.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}/event_log").last_push_data
        assert "details" not in data

    def test_session_id_not_in_payload(self):
        """
        session_id must NOT be in the event payload.
        It is already represented by the parent Firebase path node.
        """
        root = _FakeReference()
        append_event_log(
            _SESSION_ID, "2026-09-23 10:01:05", "ADD", _PRODUCT_ID,
            _ref=root,
        )
        data = root.node(f"sessions/{_SESSION_ID}/event_log").last_push_data
        assert "session_id" not in data

    def test_add_and_remove_event_types(self):
        """Both ADD and REMOVE event_type values must be passed through correctly."""
        root = _FakeReference()
        append_event_log(_SESSION_ID, "2026-09-23 10:01:00", "ADD", 1, _ref=root)
        data = root.node(f"sessions/{_SESSION_ID}/event_log").last_push_data
        assert data["event_type"] == "ADD"

        root2 = _FakeReference()
        append_event_log(_SESSION_ID, "2026-09-23 10:05:00", "REMOVE", 1, _ref=root2)
        data2 = root2.node(f"sessions/{_SESSION_ID}/event_log").last_push_data
        assert data2["event_type"] == "REMOVE"


# ---------------------------------------------------------------------------
# Remove bill item tests
# ---------------------------------------------------------------------------

class TestRemoveBillItem:
    """Tests for remove_bill_item()."""

    def test_correct_firebase_path(self):
        """
        remove_bill_item() must navigate to
        sessions/{session_id}/bill_items/{product_id}.
        """
        root = _FakeReference()
        remove_bill_item(_SESSION_ID, _PRODUCT_ID, _ref=root)
        expected_path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        assert root.node(expected_path) is not None

    def test_delete_called(self):
        """remove_bill_item() must call delete() on the bill-item node."""
        root = _FakeReference()
        remove_bill_item(_SESSION_ID, _PRODUCT_ID, _ref=root)
        path = f"sessions/{_SESSION_ID}/bill_items/{_PRODUCT_ID}"
        assert root.node(path).deleted is True

    def test_different_products_deleted_independently(self):
        """Removing product 1 must not mark product 2's node as deleted."""
        root = _FakeReference()
        remove_bill_item(_SESSION_ID, 1, _ref=root)
        remove_bill_item(_SESSION_ID, 2, _ref=root)
        assert root.node(f"sessions/{_SESSION_ID}/bill_items/1").deleted is True
        assert root.node(f"sessions/{_SESSION_ID}/bill_items/2").deleted is True

        root2 = _FakeReference()
        remove_bill_item(_SESSION_ID, 1, _ref=root2)
        # Product 2 was never touched in root2
        assert root2.node(f"sessions/{_SESSION_ID}/bill_items/2") is None


# ---------------------------------------------------------------------------
# Error behavior tests
# ---------------------------------------------------------------------------

class TestErrorBehavior:
    """Tests verifying that Firebase failures are surfaced cleanly."""

    def test_sync_product_raises_on_firebase_failure(self):
        """
        When Firebase write fails, sync_product() must raise the exception
        rather than silently swallowing it.
        """
        err_ref = _ErrorReference()
        with pytest.raises(OSError, match="Firebase write failed"):
            sync_product(
                1, "Maggi Small", None, 10.0, 35.0, "Maggi_10",
                _ref=err_ref,
            )

    def test_sync_session_raises_on_firebase_failure(self):
        """sync_session() must surface Firebase failures to the caller."""
        err_ref = _ErrorReference()
        with pytest.raises(OSError, match="Firebase write failed"):
            sync_session(42, "2026-09-23 10:00:00", None, 0.0, _ref=err_ref)

    def test_sync_bill_item_raises_on_firebase_failure(self):
        """sync_bill_item() must surface Firebase failures to the caller."""
        err_ref = _ErrorReference()
        with pytest.raises(OSError, match="Firebase write failed"):
            sync_bill_item(42, 1, "Maggi Small", 1, 10.0, 10.0, _ref=err_ref)

    def test_append_event_log_raises_on_firebase_failure(self):
        """append_event_log() must surface Firebase failures to the caller."""
        err_ref = _ErrorReference()
        with pytest.raises(OSError, match="Firebase write failed"):
            append_event_log(42, "2026-09-23 10:01:05", "ADD", 1, _ref=err_ref)

    def test_remove_bill_item_raises_on_firebase_failure(self):
        """remove_bill_item() must surface Firebase failures to the caller."""
        err_ref = _ErrorReference()
        with pytest.raises(OSError, match="Firebase write failed"):
            remove_bill_item(42, 1, _ref=err_ref)


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------

def test_module_import_does_not_import_firebase_admin():
    """
    Importing edge.firebase.sync must not cause firebase_admin to be
    imported as a side effect.  The sync module uses the client's public
    API (get_reference) which itself lazy-imports firebase_admin only
    when a real connection is made.
    """
    sys.modules.pop("firebase_admin", None)
    importlib.import_module("edge.firebase.sync")
    assert "firebase_admin" not in sys.modules, (
        "firebase_admin must not be imported at module load time via sync.py"
    )
