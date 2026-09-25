"""
Tests for session ownership: cart_id and customer_uid in billing and CartSession.

Covers:
    - Session can be created with a cart_id
    - cart_id is persisted correctly in SQLite
    - set_session_cart_id() updates existing sessions
    - assign_customer_to_session() updates existing sessions
    - get_session_meta() returns cart_id and customer_uid correctly
    - CartSession.assign_customer() persists and triggers Firebase sync
    - Session created without cart_id / customer_uid has None for both
    - Cart → Session → Customer association is represented correctly

No real Firebase project, no camera, no Raspberry Pi, no HX711.
"""

from __future__ import annotations

# pyrefly: ignore [missing-import]
import pytest

from edge.billing.billing import (
    assign_customer_to_session,
    create_session,
    get_session_meta,
    set_session_cart_id,
)
from edge.cart_session import CartSession
from edge.database import database as _db_module
from edge.database.database import initialize_database
from edge.database.product_repository import add_product, get_product_by_yolo_class
from edge.detection.product_resolver import Detection, ProductInfo, ProductResolver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite database in a temp directory."""
    temp_db = tmp_path / "test_ownership.db"
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


def _det(yolo_class: str = "Maggi_10", confidence: float = 0.9) -> Detection:
    return Detection(yolo_class=yolo_class, confidence=confidence)


class _FakeReference:
    """Minimal fake Firebase reference for ownership tests."""

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.last_set_data: dict | None = None
        self.last_update_data: dict | None = None
        self.last_push_data: dict | None = None
        self.deleted: bool = False
        self._children: dict[str, _FakeReference] = {}

    def child(self, subpath: str) -> _FakeReference:
        if subpath not in self._children:
            self._children[subpath] = _FakeReference(path=subpath)
        return self._children[subpath]

    def set(self, data: dict) -> None:
        self.last_set_data = data

    def update(self, data: dict) -> None:
        self.last_update_data = data

    def push(self, data: dict) -> None:
        self.last_push_data = data

    def delete(self) -> None:
        self.deleted = True

    def node(self, subpath: str) -> _FakeReference | None:
        return self._children.get(subpath)


# ---------------------------------------------------------------------------
# billing.create_session — cart_id parameter
# ---------------------------------------------------------------------------

class TestCreateSessionWithCartId:

    def test_session_created_without_cart_id_has_none(self):
        """A session created without cart_id must have cart_id=None."""
        sid = create_session()
        meta = get_session_meta(sid)
        assert meta is not None
        assert meta["cart_id"] is None

    def test_session_created_with_cart_id_stores_it(self):
        """A session created with a cart_id must persist it to SQLite."""
        sid = create_session(cart_id="CART-XYZ")
        meta = get_session_meta(sid)
        assert meta is not None
        assert meta["cart_id"] == "CART-XYZ"

    def test_session_created_without_customer_uid_has_none(self):
        """A new session must have customer_uid=None until assigned."""
        sid = create_session()
        meta = get_session_meta(sid)
        assert meta["customer_uid"] is None

    def test_multiple_sessions_have_independent_cart_ids(self):
        """Two sessions must have independent cart_ids."""
        sid_a = create_session(cart_id="CART-A")
        sid_b = create_session(cart_id="CART-B")
        assert get_session_meta(sid_a)["cart_id"] == "CART-A"
        assert get_session_meta(sid_b)["cart_id"] == "CART-B"


# ---------------------------------------------------------------------------
# billing.set_session_cart_id
# ---------------------------------------------------------------------------

class TestSetSessionCartId:

    def test_set_cart_id_updates_sqlite(self):
        """set_session_cart_id() must persist the cart_id to SQLite."""
        sid = create_session()
        set_session_cart_id(sid, "CART-42")
        meta = get_session_meta(sid)
        assert meta["cart_id"] == "CART-42"

    def test_set_cart_id_does_not_affect_other_sessions(self):
        """Updating cart_id for one session must not affect another."""
        sid_a = create_session()
        sid_b = create_session()
        set_session_cart_id(sid_a, "CART-A")
        assert get_session_meta(sid_b)["cart_id"] is None

    def test_set_cart_id_can_be_called_after_creation(self):
        """cart_id can be set after session creation (lazy assignment)."""
        sid = create_session()
        assert get_session_meta(sid)["cart_id"] is None
        set_session_cart_id(sid, "LATE-CART")
        assert get_session_meta(sid)["cart_id"] == "LATE-CART"


# ---------------------------------------------------------------------------
# billing.assign_customer_to_session
# ---------------------------------------------------------------------------

class TestAssignCustomerToSession:

    def test_assign_customer_stores_uid(self):
        """assign_customer_to_session() must persist customer_uid to SQLite."""
        sid = create_session()
        assign_customer_to_session(sid, "uid-abc-123")
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-abc-123"

    def test_assign_customer_does_not_affect_other_sessions(self):
        """Assigning a customer to one session must not affect another."""
        sid_a = create_session()
        sid_b = create_session()
        assign_customer_to_session(sid_a, "uid-a")
        assert get_session_meta(sid_b)["customer_uid"] is None

    def test_customer_and_cart_are_independent(self):
        """cart_id and customer_uid can be set independently."""
        sid = create_session(cart_id="CART-77")
        assign_customer_to_session(sid, "uid-xyz")
        meta = get_session_meta(sid)
        assert meta["cart_id"] == "CART-77"
        assert meta["customer_uid"] == "uid-xyz"


# ---------------------------------------------------------------------------
# billing.get_session_meta
# ---------------------------------------------------------------------------

class TestGetSessionMeta:

    def test_returns_none_for_missing_session(self):
        """get_session_meta() must return None for a non-existent session_id."""
        meta = get_session_meta(99999)
        assert meta is None

    def test_returns_all_required_keys(self):
        """get_session_meta() must return all required keys."""
        sid = create_session()
        meta = get_session_meta(sid)
        required = {"session_id", "start_time", "end_time", "total_amount",
                    "cart_id", "customer_uid"}
        assert set(meta.keys()) == required

    def test_start_time_is_not_none(self):
        """start_time must be set (datetime('now')) at session creation."""
        sid = create_session()
        meta = get_session_meta(sid)
        assert meta["start_time"] is not None
        assert len(meta["start_time"]) > 0

    def test_end_time_is_none_for_active_session(self):
        """end_time must be None for an active (not completed) session."""
        sid = create_session()
        meta = get_session_meta(sid)
        assert meta["end_time"] is None

    def test_total_amount_is_zero_for_new_session(self):
        """A new session must have total_amount=0."""
        sid = create_session()
        meta = get_session_meta(sid)
        assert meta["total_amount"] == 0


# ---------------------------------------------------------------------------
# CartSession — cart_id and customer_uid via constructor
# ---------------------------------------------------------------------------

class TestCartSessionOwnership:

    def test_cart_session_creates_session_with_cart_id(self):
        """CartSession created with cart_id must store it in SQLite."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        session = CartSession(resolver, cart_id="CART-001")
        meta = get_session_meta(session.session_id)
        assert meta["cart_id"] == "CART-001"

    def test_cart_session_no_cart_id_stores_none(self):
        """CartSession created without cart_id must have cart_id=None."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        session = CartSession(resolver)
        meta = get_session_meta(session.session_id)
        assert meta["cart_id"] is None

    def test_assign_customer_persists_uid(self):
        """CartSession.assign_customer() must persist customer_uid to SQLite."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        session = CartSession(resolver)
        session.assign_customer("uid-customer-1")
        meta = get_session_meta(session.session_id)
        assert meta["customer_uid"] == "uid-customer-1"

    def test_assign_customer_triggers_firebase_sync(self):
        """CartSession.assign_customer() must sync session meta to Firebase."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        root = _FakeReference()
        session = CartSession(resolver, cart_id="CART-QR1", firebase_ref=root)
        session.assign_customer("uid-firebase-user")

        node = root.node(f"sessions/{session.session_id}")
        assert node is not None, "Firebase session node must be created"
        data = node.last_update_data
        assert data is not None, "update() must be called on session node"
        assert data["customer_uid"] == "uid-firebase-user"
        assert data["cart_id"] == "CART-QR1"

    def test_cart_id_in_firebase_session_sync(self):
        """cart_id must appear in the Firebase session sync payload."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        root = _FakeReference()
        session = CartSession(resolver, cart_id="CART-QR2", firebase_ref=root)

        # Trigger a sync by assigning a customer
        session.assign_customer("uid-test")
        node = root.node(f"sessions/{session.session_id}")
        assert node.last_update_data["cart_id"] == "CART-QR2"

    def test_pre_existing_session_with_cart_id(self):
        """
        CartSession initialized with an existing session_id and a cart_id
        must update the existing session row's cart_id.
        """
        sid = create_session()
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        CartSession(resolver, session_id=sid, cart_id="CART-RETRO")
        meta = get_session_meta(sid)
        assert meta["cart_id"] == "CART-RETRO"

    def test_two_sessions_independent_ownership(self):
        """Two CartSessions must have independent cart_id and customer_uid."""
        maggi = _seed_product()
        resolver = ProductResolver([maggi])
        session_a = CartSession(resolver, cart_id="CART-A")
        session_b = CartSession(resolver, cart_id="CART-B")
        session_a.assign_customer("uid-a")
        session_b.assign_customer("uid-b")

        meta_a = get_session_meta(session_a.session_id)
        meta_b = get_session_meta(session_b.session_id)

        assert meta_a["cart_id"] == "CART-A"
        assert meta_b["cart_id"] == "CART-B"
        assert meta_a["customer_uid"] == "uid-a"
        assert meta_b["customer_uid"] == "uid-b"


class TestOwnershipAuthorizationContract:
    def test_ownership_survives_billing_sync_and_completion(self):
        product = _seed_product()
        root = _FakeReference()
        session = CartSession(ProductResolver([product]), cart_id="CART-A", firebase_ref=root)
        session.assign_customer("uid-owner")
        assert session.process_add([_det()], weight_change=75.0).success
        node = root.node(f"sessions/{session.session_id}")
        assert node.last_update_data["customer_uid"] == "uid-owner"
        assert node.last_update_data["cart_id"] == "CART-A"
        assert node.last_update_data["total_amount"] == 10.0
        assert node.last_update_data["end_time"] is None
        session.complete()
        assert node.last_update_data["end_time"] is not None
        assert node.last_update_data["customer_uid"] == "uid-owner"
        assert get_session_meta(session.session_id)["customer_uid"] == "uid-owner"
        assert not node.deleted  # Completed history is retained, still owned.

    def test_assignment_sync_failure_does_not_erase_sqlite_ownership(self, monkeypatch):
        product = _seed_product()
        root = _FakeReference()
        session = CartSession(ProductResolver([product]), cart_id="CART-A", firebase_ref=root)
        node = root.child(f"sessions/{session.session_id}")

        def fail_update(data):
            raise RuntimeError("simulated sync failure")

        monkeypatch.setattr(node, "update", fail_update)
        with pytest.raises(RuntimeError, match="simulated sync failure"):
            session.assign_customer("uid-owner")
        assert get_session_meta(session.session_id)["customer_uid"] == "uid-owner"
        assert node.last_update_data is None
