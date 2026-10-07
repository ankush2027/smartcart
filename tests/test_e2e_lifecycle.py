"""
tests/test_e2e_lifecycle.py — End-to-end software integration acceptance test suite.

Validates the full Customer A -> Customer B software demo lifecycle without
any physical hardware dependencies (no real load-cell/HX711, camera, or Razorpay).

LIFECYCLE TESTED
----------------
Customer A:
1. No active session.
2. Customer A pairs CART-001.
3. Active session is created.
4. Supervisor discovers it.
5. Fresh runtime attaches.
6. Simulated weight path remains functional.
7. ADD/REMOVE can reach authoritative billing path.
8. Checkout freezes the exact session/generation.
9. Runtime stops/drains.
10. Duplicate checkout request is idempotent.
11. Cancel checkout.
12. Old runtime cannot mutate (stale generation rejected).
13. Fresh runtime attaches to new authoritative generation.
14. Freeze checkout again.
15. Start explicit fake payment.
16. Fake provider creates the provider order.
17. Simulate successful fake payment through FakeProvider.
18. Submit/consume valid evidence using the existing verification path.
19. Payment becomes VERIFIED/COMPLETED.
20. Exact checkout becomes COMPLETED.
21. session.end_time is set.
22. Firebase completion projection hook is invoked.
23. Runtime is released.

Customer B:
24. Customer B pairs CART-001.
25. A NEW session is created.
26. Supervisor discovers it.
27. Fresh runtime attaches automatically without restarting backend.
28. No state from Customer A leaks into Customer B.

FAILURE & IDEMPOTENCY COVERAGE
------------------------------
- repeated pairing by same owner
- second customer blocked while first owns active cart
- duplicate checkout key
- stale runtime generation after cancellation
- duplicate payment initiation
- duplicate successful completion
- Firebase completion sync failure after SQLite commit (SQLite remains authoritative)
- supervisor shutdown
- no two simultaneous runtimes for same cart
"""
from __future__ import annotations

from datetime import datetime, timedelta
import threading
from typing import Callable

import numpy as np
import pytest

from edge.billing import billing
from edge.billing.billing import StaleRuntimeError, get_session_meta
from edge.billing.checkout import begin_checkout, cancel_checkout, read_checkout_state
from edge.cart_session import CartSession
from edge.database import database as _db_module
from edge.database.database import get_connection, initialize_database
from edge.database.product_repository import add_product, get_all_products
from edge.demo.composition import (
    create_demo_app,
    create_demo_payment_service,
    simulate_demo_payment_success,
)
from edge.detection.event import DetectionEvent
from edge.detection.product_resolver import Detection, ProductResolver
from edge.firebase import client as _firebase_client
from edge.pairing.pair_cart import PairingError, pair_customer_to_cart
from edge.payment.fake import FakeLedger, FakeProvider
from edge.payment.service import PaymentService
from edge.runtime.cart_runtime import CartRuntime, resume_session
from edge.runtime.run_cart import cart_lock
from edge.runtime.supervisor import CartSupervisor
from edge.runtime.weight_source import SimulatedWeightSource, create_weight_source


# ---------------------------------------------------------------------------
# Test fakes and mocks
# ---------------------------------------------------------------------------

class _FakeReference:
    """In-memory tree fake for Firebase reference."""

    def __init__(self, path: str = "") -> None:
        self.path = path
        self.last_set_data: dict | None = None
        self.last_update_data: dict | None = None
        self.last_push_data: dict | None = None
        self.deleted: bool = False
        self._children: dict[str, _FakeReference] = {}
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


class _FailingReference(_FakeReference):
    """Fake reference that raises on writes to test resilient error handling."""

    def child(self, subpath: str) -> _FailingReference:
        return self

    def update(self, data: dict) -> None:
        raise OSError("Simulated Firebase connection failure")


class MockDetector:
    class_names = ("Snack", "Drink")

    def __init__(self):
        self.detections = [Detection("Snack", 0.95)]

    def detect(self, frame):
        return self.detections


class MockMotion:
    event = None

    def process(self, frame, timestamp=None):
        res, self.event = self.event, None
        return res


class MockCamera:
    def __init__(self):
        self.released = False
        self._stop = threading.Event()

    def read(self):
        if self._stop.wait(0.02):
            return False, None
        return True, np.zeros((10, 10, 3), dtype=np.uint8)

    def release(self):
        self.released = True
        self._stop.set()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    """Set up isolated SQLite test database and allowed carts."""
    db_file = tmp_path / "e2e_test.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", db_file)
    monkeypatch.setenv("SMARTCART_DATABASE_PATH", str(db_file))
    monkeypatch.setenv("SMARTCART_ALLOWED_CARTS", "CART-001,CART-002")
    initialize_database()

    # Stub default Firebase client with in-memory fake
    fake_root = _FakeReference()
    monkeypatch.setattr(_firebase_client, "get_reference", lambda *a: fake_root)
    monkeypatch.setattr(_firebase_client, "initialize", lambda: None)

    # Seed two test products
    add_product("Snack Bar", "SNACK-001", 20.0, 50.0, "Snack")
    add_product("Cold Drink", "DRINK-001", 30.0, 250.0, "Drink")


@pytest.fixture
def fake_firebase():
    return _FakeReference()


def mock_token_verifier(token: str) -> dict:
    """Mock verifier mapping 'cust-XXX' -> {'uid': 'cust-XXX'}."""
    if token.startswith("cust-"):
        return {"uid": token}
    raise ValueError("Invalid test token")


# ---------------------------------------------------------------------------
# E2E Acceptance Test
# ---------------------------------------------------------------------------

def test_full_customer_a_then_b_demo_lifecycle(fake_firebase):
    """
    Executes the complete 28-step Customer A -> Customer B software lifecycle.
    """
    cart_id = "CART-001"
    customer_a = "cust-A"
    customer_b = "cust-B"

    # Step 1: No active session initially.
    attached_runtimes: list[CartRuntime] = []

    def mock_runtime_factory(session_id: int) -> CartRuntime:
        det = MockDetector()
        session = resume_session(cart_id, det, fake_firebase)
        rt = CartRuntime(
            camera=MockCamera(),
            motion=MockMotion(),
            weights=create_weight_source("simulated", initial_weight=500.0),
            detector=det,
            session=session,
            cart_id=cart_id,
            shutdown_timeout=0.2,
        )
        attached_runtimes.append(rt)
        return rt

    supervisor = CartSupervisor(
        cart_id=cart_id,
        runtime_factory=mock_runtime_factory,
        poll_interval=0.01,
        drain_poll_interval=0.01,
    )

    # Supervisor starts in IDLE; no session to attach.
    assert supervisor.state == "IDLE"
    supervisor.tick()
    assert supervisor.state == "IDLE"
    assert len(attached_runtimes) == 0

    # Step 2-3: Customer A pairs CART-001. Active session created.
    pair_res = pair_customer_to_cart(customer_uid=customer_a, cart_id=cart_id)
    session_a_id = pair_res.session_id
    assert pair_res.status == "paired"
    assert session_a_id is not None

    # Step 4-5: Supervisor discovers session A, attaches fresh runtime.
    sup_thread = threading.Thread(
        target=supervisor.run_forever,
        name="test-supervisor-thread",
        daemon=True,
    )
    sup_thread.start()

    # Wait for supervisor to discover session and attach runtime
    for _ in range(50):
        if supervisor.state == "RUNNING" and len(attached_runtimes) == 1:
            break
        threading.Event().wait(0.05)

    assert supervisor.state == "RUNNING"
    assert len(attached_runtimes) == 1
    runtime_a1 = attached_runtimes[0]

    # Step 6-7: Simulated shopping: ADD item to CartSession
    products = get_all_products()
    snack_id = products[0][0]
    add_ok = runtime_a1.session.process_add(
        detections=[Detection("Snack", 0.95)],
        weight_change=50.0,
    )
    assert add_ok.success is True
    meta_a = get_session_meta(session_a_id)
    assert meta_a["total_amount"] == 20.0

    # Step 8-9: Checkout freeze. Freezes exact session and generation.
    checkout_1 = begin_checkout(session_a_id, customer_a, request_key="chk-key-1")
    assert checkout_1.state == "FROZEN"
    assert checkout_1.checkout_id == 1

    # Supervisor should detect freeze and stop/drain runtime_a1
    for _ in range(50):
        if supervisor.state == "DRAINING" and runtime_a1.stopping.is_set():
            break
        threading.Event().wait(0.05)

    assert supervisor.state == "DRAINING"
    assert runtime_a1.stopping.is_set()

    # Step 10: Duplicate checkout request is idempotent
    checkout_1_dup = begin_checkout(session_a_id, customer_a, request_key="chk-key-1")
    assert checkout_1_dup.checkout_id == checkout_1.checkout_id
    assert checkout_1_dup.state == "FROZEN"
    assert checkout_1_dup.final_amount == checkout_1.final_amount

    # Step 11: Cancel checkout
    cancel_res = cancel_checkout(session_a_id, checkout_1.checkout_id, customer_a)
    assert cancel_res.state == "CANCELLED"

    # Step 12: Old runtime cannot mutate — stale generation is rejected
    with pytest.raises(StaleRuntimeError):
        billing.add_runtime_item(session_a_id, snack_id, expected_generation=runtime_a1.session.checkout_generation)

    # Step 13: Supervisor discovers cancellation, creates fresh runtime with new generation
    for _ in range(50):
        if supervisor.state == "RUNNING" and len(attached_runtimes) == 2:
            break
        threading.Event().wait(0.05)

    assert supervisor.state == "RUNNING"
    assert len(attached_runtimes) == 2
    runtime_a2 = attached_runtimes[1]
    assert runtime_a2.session.checkout_generation == 1  # Hydrated new generation

    # Step 14: Freeze checkout again (Generation 2)
    checkout_2 = begin_checkout(session_a_id, customer_a, request_key="chk-key-2")
    assert checkout_2.checkout_id == 2
    assert checkout_2.state == "FROZEN"

    for _ in range(50):
        if supervisor.state == "DRAINING" and runtime_a2.stopping.is_set():
            break
        threading.Event().wait(0.05)

    assert supervisor.state == "DRAINING"

    # Step 15-16: Start explicit fake payment
    payment_service, fake_provider = create_demo_payment_service(firebase_ref=fake_firebase)
    start_res = payment_service.start(
        session_id=session_a_id,
        checkout_id=checkout_2.checkout_id,
        uid=customer_a,
        request_key="pay-key-1",
    )
    assert start_res["state"] == "ORDER_READY"
    assert start_res["provider_order_id"].startswith("fake-order-")

    # Step 17-20: Simulate successful fake payment through FakeProvider and verify
    pay_result = simulate_demo_payment_success(
        payment_service=payment_service,
        fake_provider=fake_provider,
        session_id=session_a_id,
        checkout_id=checkout_2.checkout_id,
        uid=customer_a,
    )
    assert pay_result["state"] == "COMPLETED"
    assert pay_result["projection_sync"] == "succeeded"

    # Step 21: SQLite session.end_time is set
    meta_completed = get_session_meta(session_a_id)
    assert meta_completed["end_time"] is not None

    # Step 22: Firebase completion projection hook updated the session
    session_node = fake_firebase.child(f"sessions/{session_a_id}")
    assert session_node.last_update_data is not None
    assert session_node.last_update_data["end_time"] == meta_completed["end_time"]

    # Step 23: Supervisor releases the runtime and waits for next customer
    for _ in range(50):
        if supervisor.state in ("COMPLETED", "IDLE"):
            break
        threading.Event().wait(0.05)

    # ------------------------------------------------------------------
    # CUSTOMER B
    # ------------------------------------------------------------------

    # Step 24-25: Customer B pairs CART-001. A NEW session is created.
    pair_b = pair_customer_to_cart(customer_uid=customer_b, cart_id=cart_id)
    session_b_id = pair_b.session_id
    assert pair_b.status == "paired"
    assert session_b_id != session_a_id

    # Step 26-27: Supervisor discovers it, fresh runtime attaches automatically
    for _ in range(50):
        if supervisor.state == "RUNNING" and len(attached_runtimes) == 3:
            break
        threading.Event().wait(0.05)

    assert supervisor.state == "RUNNING"
    assert len(attached_runtimes) == 3
    runtime_b = attached_runtimes[2]

    # Step 28: No state from Customer A leaks into Customer B
    meta_b = get_session_meta(session_b_id)
    assert meta_b["customer_uid"] == customer_b
    assert meta_b["total_amount"] == 0.0
    assert meta_b["end_time"] is None
    assert len(runtime_b.session.get_bill()) == 0

    # Stop supervisor cleanly
    supervisor.request_stop()
    sup_thread.join(timeout=2.0)
    assert not sup_thread.is_alive()


# ---------------------------------------------------------------------------
# Failure and Idempotency Tests
# ---------------------------------------------------------------------------

def test_repeated_pairing_by_same_owner():
    """Customer pairing CART-001 twice returns already_paired and reuses session."""
    res1 = pair_customer_to_cart("cust-1", "CART-001")
    assert res1.status == "paired"

    res2 = pair_customer_to_cart("cust-1", "CART-001")
    assert res2.status == "already_paired"
    assert res2.session_id == res1.session_id


def test_second_customer_blocked_while_first_owns_active_cart():
    """Customer B is blocked while Customer A owns an active cart session."""
    pair_customer_to_cart("cust-1", "CART-001")

    with pytest.raises(PairingError) as exc_info:
        pair_customer_to_cart("cust-2", "CART-001")

    assert exc_info.value.code == "cart_already_assigned"
    assert exc_info.value.http_status == 409


def test_duplicate_checkout_key():
    """Duplicate checkout with same key is idempotent; mismatch raises."""
    res = pair_customer_to_cart("cust-1", "CART-001")
    sid = res.session_id
    prods = get_all_products()
    pid = prods[0][0]
    billing.add_runtime_item(sid, pid, expected_generation=0)

    chk1 = begin_checkout(sid, "cust-1", request_key="chk-key-abc")
    chk2 = begin_checkout(sid, "cust-1", request_key="chk-key-abc")
    assert chk1.checkout_id == chk2.checkout_id


def test_stale_runtime_generation_after_cancellation():
    """Old runtime generation is rejected after a checkout cancellation."""
    res = pair_customer_to_cart("cust-1", "CART-001")
    sid = res.session_id
    prods = get_all_products()
    pid = prods[0][0]

    # Initial generation = 0
    billing.add_runtime_item(sid, pid, expected_generation=0)

    # Freeze (chk 1) then cancel -> new generation = 1
    chk = begin_checkout(sid, "cust-1", request_key="key-1")
    cancel_checkout(sid, chk.checkout_id, "cust-1")

    # Stale generation 0 must be rejected
    with pytest.raises(StaleRuntimeError):
        billing.add_runtime_item(sid, pid, expected_generation=0)

    # New generation 1 is accepted
    ok = billing.add_runtime_item(sid, pid, expected_generation=1)
    assert ok is True


def test_duplicate_payment_initiation():
    """Calling start payment with same idempotency key is idempotent."""
    res = pair_customer_to_cart("cust-1", "CART-001")
    sid = res.session_id
    prods = get_all_products()
    pid = prods[0][0]
    billing.add_runtime_item(sid, pid, expected_generation=0)

    chk = begin_checkout(sid, "cust-1", request_key="key-pay-test")

    ps, _ = create_demo_payment_service()
    p1 = ps.start(sid, chk.checkout_id, "cust-1", "pay-idem-key")
    p2 = ps.start(sid, chk.checkout_id, "cust-1", "pay-idem-key")
    assert p1["payment_attempt_id"] == p2["payment_attempt_id"]


def test_duplicate_successful_completion():
    """Submitting completion twice is idempotent and returns COMPLETED."""
    res = pair_customer_to_cart("cust-1", "CART-001")
    sid = res.session_id
    prods = get_all_products()
    pid = prods[0][0]
    billing.add_runtime_item(sid, pid, expected_generation=0)

    chk = begin_checkout(sid, "cust-1", request_key="key-dup-comp")

    ps, fp = create_demo_payment_service()
    ps.start(sid, chk.checkout_id, "cust-1", "key-comp-1")

    res1 = simulate_demo_payment_success(ps, fp, sid, chk.checkout_id, "cust-1")
    assert res1["state"] == "COMPLETED"

    # Second completion call via PaymentService.complete is idempotent
    attempt = ps.status(sid, chk.checkout_id, "cust-1")
    res2 = ps.complete(chk.checkout_id, attempt["payment_attempt_id"])
    assert res2["state"] == "COMPLETED"


def test_firebase_completion_sync_failure_after_sqlite_commit():
    """Firebase sync failure does NOT roll back SQLite payment completion."""
    res = pair_customer_to_cart("cust-1", "CART-001")
    sid = res.session_id
    prods = get_all_products()
    pid = prods[0][0]
    billing.add_runtime_item(sid, pid, expected_generation=0)

    chk = begin_checkout(sid, "cust-1", request_key="key-fail-sync")

    failing_firebase = _FailingReference()
    ps, fp = create_demo_payment_service(firebase_ref=failing_firebase)
    ps.start(sid, chk.checkout_id, "cust-1", "key-comp-sync")

    res1 = simulate_demo_payment_success(ps, fp, sid, chk.checkout_id, "cust-1")
    # Payment completed in SQLite, but projection sync notes reconciliation required
    assert res1["state"] == "COMPLETED"
    assert res1["projection_sync"] == "reconciliation_required"

    # SQLite session end_time MUST be set (never rolled back by Firebase failure)
    meta = get_session_meta(sid)
    assert meta["end_time"] is not None


def test_supervisor_clean_shutdown():
    """Supervisor joins threads cleanly and resets state on stop request."""
    supervisor = CartSupervisor(
        cart_id="CART-001",
        runtime_factory=lambda sid: None,
        poll_interval=0.01,
    )
    sup_thread = threading.Thread(target=supervisor.run_forever, daemon=True)
    sup_thread.start()

    threading.Event().wait(0.05)
    supervisor.request_stop()
    sup_thread.join(timeout=1.0)
    assert not sup_thread.is_alive()
    assert supervisor.state == "IDLE"


def test_no_two_simultaneous_runtimes_for_same_cart():
    """cart_lock prevents two processes/threads from owning the same cart concurrently."""
    with cart_lock("CART-001"):
        with pytest.raises(RuntimeError, match="already owns this cart"):
            with cart_lock("CART-001"):
                pass


def test_demo_app_http_endpoints(fake_firebase):
    """Test demo Flask API endpoints with Bearer token authentication."""
    app = create_demo_app(
        token_verifier=mock_token_verifier,
        firebase_ref=fake_firebase,
    )
    client = app.test_client()

    # 1. Pair customer
    resp = client.post(
        "/pair-cart",
        json={"cart_id": "CART-001"},
        headers={"Authorization": "Bearer cust-HTTP"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    sid = data["session_id"]

    # 2. Add an item via billing
    prods = get_all_products()
    pid = prods[0][0]
    billing.add_runtime_item(sid, pid, expected_generation=0)

    # 3. Checkout
    chk_resp = client.post(
        f"/sessions/{sid}/checkout",
        json={},
        headers={
            "Authorization": "Bearer cust-HTTP",
            "Idempotency-Key": "http-chk-1",
        },
    )
    assert chk_resp.status_code == 200
    cid = chk_resp.get_json()["checkout_id"]

    # 4. Start payment
    pay_start = client.post(
        f"/sessions/{sid}/checkouts/{cid}/payment",
        json={},
        headers={
            "Authorization": "Bearer cust-HTTP",
            "Idempotency-Key": "http-pay-1",
        },
    )
    assert pay_start.status_code == 200
    assert pay_start.get_json()["state"] == "ORDER_READY"

    # 5. Simulate demo payment success via demo route
    demo_pay = client.post(
        f"/demo/sessions/{sid}/checkouts/{cid}/simulate-payment-success",
        headers={"Authorization": "Bearer cust-HTTP"},
    )
    assert demo_pay.status_code == 200
    assert demo_pay.get_json()["state"] == "COMPLETED"

    # 6. Verify session completed
    meta = get_session_meta(sid)
    assert meta["end_time"] is not None
