"""
tests/test_pair_cart.py — Comprehensive offline tests for the SmartCart pairing system.

Coverage
--------
Authentication
    - missing bearer token
    - malformed Authorization header (no "Bearer " prefix)
    - invalid / rejected token
    - expired token
    - verified token produces UID; request body UID cannot impersonate

Pairing
    - successful unassigned cart pairing
    - same customer + same cart is idempotent (via pre-check path)
    - same customer + same cart is idempotent (via session ownership path)
    - cart already assigned to another customer rejected
    - customer already owns another active cart rejected
    - nonexistent cart rejected
    - eligible cart without active session creates a session
    - completed session not considered active
    - multiple active sessions for cart rejected
    - multiple active sessions for customer rejected
    - empty cart_id rejected
    - whitespace-only cart_id rejected
    - missing cart_id in body rejected
    - non-JSON body rejected

Persistence
    - successful assignment persists customer_uid to SQLite
    - Firebase synchronization receives correct UID, session_id, cart_id
    - Firebase sync failure does NOT erase successful SQLite ownership
    - completed/history sessions remain intact after pairing

Concurrency/invariants
    - assign_customer_atomic raises IntegrityConflict on concurrent race

Regression
    - All existing test suites must still pass (verified separately by
      running the full suite; this file adds to it).

No real Firebase project, no camera, no Raspberry Pi, no HX711.
"""

from __future__ import annotations

import sqlite3
from typing import Callable
from unittest.mock import MagicMock

# pyrefly: ignore [missing-import]
import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from edge.pairing.token_verifier import TokenVerificationError, verify_id_token
from edge.pairing.session_store import (
    IntegrityConflict,
    assign_customer_atomic,
    get_active_session_for_cart,
    get_active_session_for_customer,
)
from edge.pairing.pair_cart import PairingError, PairingResult, pair_customer_to_cart
from edge.api.server import create_app
from edge.billing.billing import (
    assign_customer_to_session,
    complete_session,
    create_session,
    get_session_meta,
)
from edge.database import database as _db_module
from edge.database.database import initialize_database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite database in a temp directory."""
    temp_db = tmp_path / "test_pairing.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", temp_db)
    monkeypatch.setenv("SMARTCART_ALLOWED_CARTS", "CART-1,CART-2,CART-A,CART-B,CART-DUP,CART-UNUSED")
    initialize_database()
    return temp_db


@pytest.fixture()
def db_conn(isolated_db):
    """Return an open connection to the isolated test database."""
    conn = sqlite3.connect(str(isolated_db))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Fake verifier helpers
# ---------------------------------------------------------------------------

def _verifier_ok(uid: str) -> Callable:
    """Return a verifier callable that always returns *uid*."""
    def _verify(token: str) -> dict:
        return {"uid": uid}
    return _verify


def _verifier_error(exc: Exception) -> Callable:
    """Return a verifier callable that always raises *exc*."""
    def _verify(token: str):
        raise exc
    return _verify


class _FakeFirebaseError(Exception):
    """Simulates firebase_admin.auth errors."""


# ---------------------------------------------------------------------------
# Fake sync helpers
# ---------------------------------------------------------------------------

class _CapturingSync:
    """Records calls to the Firebase sync function."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.raise_on_next: Exception | None = None

    def __call__(self, session_id: int, customer_uid: str, cart_id: str) -> None:
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        self.calls.append((session_id, customer_uid, cart_id))

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# Flask test client helpers
# ---------------------------------------------------------------------------

def _make_client(uid: str, sync: _CapturingSync | None = None):
    """Create a Flask test client with a fake verifier producing *uid*."""
    app = create_app(
        token_verifier=_verifier_ok(uid),
        firebase_sync_fn=sync or _CapturingSync(),
    )
    app.config["TESTING"] = True
    return app.test_client()


def _make_client_with_verifier(verifier, sync=None):
    """Create a Flask test client with a custom verifier callable."""
    app = create_app(
        token_verifier=verifier,
        firebase_sync_fn=sync or _CapturingSync(),
    )
    app.config["TESTING"] = True
    return app.test_client()


def _pair(client, cart_id, auth="Bearer valid-token"):
    """Helper: POST /pair-cart."""
    headers = {"Authorization": auth} if auth else {}
    return client.post(
        "/pair-cart",
        json={"cart_id": cart_id},
        headers=headers,
    )


# ===========================================================================
# Authentication tests
# ===========================================================================

class TestAuthentication:

    def test_missing_authorization_header_returns_401(self):
        client = _make_client("uid-x")
        resp = client.post("/pair-cart", json={"cart_id": "CART-1"})
        assert resp.status_code == 401
        data = resp.get_json()
        assert data["error"] == "authentication_required"

    def test_no_bearer_prefix_returns_401(self):
        client = _make_client("uid-x")
        resp = client.post(
            "/pair-cart",
            json={"cart_id": "CART-1"},
            headers={"Authorization": "Token some-token"},
        )
        assert resp.status_code == 401
        data = resp.get_json()
        assert data["error"] == "authentication_required"

    def test_empty_bearer_token_raises_authentication_required(self):
        """An empty token after "Bearer " must be rejected."""
        app = create_app(
            token_verifier=_verifier_ok("uid-x"),
            firebase_sync_fn=_CapturingSync(),
        )
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.post(
            "/pair-cart",
            json={"cart_id": "CART-1"},
            headers={"Authorization": "Bearer "},
        )
        # Empty string after stripping "Bearer " — TokenVerificationError
        assert resp.status_code in (401, 403)
        assert resp.get_json()["error"] in (
            "authentication_required", "invalid_token"
        )

    def test_invalid_token_returns_403(self):
        """A token rejected by the verifier produces HTTP 403."""
        verifier = _verifier_error(_FakeFirebaseError("bad sig"))
        client = _make_client_with_verifier(verifier)
        create_session(cart_id="CART-1")
        resp = _pair(client, "CART-1")
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["error"] == "invalid_token"

    def test_expired_token_returns_403(self):
        """An expired-token error from the verifier produces HTTP 403."""
        verifier = _verifier_error(_FakeFirebaseError("expired"))
        client = _make_client_with_verifier(verifier)
        create_session(cart_id="CART-1")
        resp = _pair(client, "CART-1")
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "invalid_token"

    def test_verified_token_uid_is_used_not_body(self):
        """
        The UID comes from the verified token, not from a body field.
        Even if the body contains a 'customer_uid', it must be ignored.
        """
        sync = _CapturingSync()
        uid_from_token = "uid-real"
        app = create_app(
            token_verifier=_verifier_ok(uid_from_token),
            firebase_sync_fn=sync,
        )
        app.config["TESTING"] = True
        client = app.test_client()

        create_session(cart_id="CART-1")

        # Body contains a fake UID; it must NOT be used.
        resp = client.post(
            "/pair-cart",
            json={"cart_id": "CART-1", "customer_uid": "uid-attacker"},
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 200
        # Verify SQLite has the token UID, not the body UID.
        sid = resp.get_json()["session_id"]
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == uid_from_token
        assert meta["customer_uid"] != "uid-attacker"

    def test_token_verifier_abstraction_works_offline(self):
        """verify_id_token with _verifier injection never calls firebase_admin."""
        uid = verify_id_token("any-token", _verifier=_verifier_ok("uid-abc"))
        assert uid == "uid-abc"

    def test_verify_id_token_empty_raises_authentication_required(self):
        with pytest.raises(TokenVerificationError) as exc_info:
            verify_id_token("")
        assert exc_info.value.code == "authentication_required"

    def test_verify_id_token_none_raises_authentication_required(self):
        with pytest.raises(TokenVerificationError) as exc_info:
            verify_id_token(None)
        assert exc_info.value.code == "authentication_required"

    def test_verify_id_token_bad_verifier_raises_invalid_token(self):
        with pytest.raises(TokenVerificationError) as exc_info:
            verify_id_token("token", _verifier=_verifier_error(ValueError("bad")))
        assert exc_info.value.code == "invalid_token"


# ===========================================================================
# Pairing tests
# ===========================================================================

class TestPairing:

    def test_successful_unassigned_cart_pairing(self):
        """A customer can pair with an unassigned active cart."""
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        create_session(cart_id="CART-1")

        resp = _pair(client, "CART-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cart_id"] == "CART-1"
        assert data["status"] == "paired"
        assert isinstance(data["session_id"], int)

    def test_idempotent_same_customer_same_cart(self):
        """Pairing the same customer to the same cart twice returns already_paired."""
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        create_session(cart_id="CART-1")

        resp1 = _pair(client, "CART-1")
        assert resp1.status_code == 200
        assert resp1.get_json()["status"] == "paired"

        resp2 = _pair(client, "CART-1")
        assert resp2.status_code == 200
        assert resp2.get_json()["status"] == "already_paired"

    def test_idempotent_via_ownership_path(self):
        """
        When the session is already assigned to the same UID in SQLite
        (e.g. a previous pair call) and the customer has no recorded
        active session via the customer-lookup path, idempotency still works.
        """
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-alice")

        sync = _CapturingSync()
        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert result.status == "already_paired"
        assert result.session_id == sid

    def test_cart_already_assigned_to_other_customer_rejected(self):
        """A cart already owned by uid-bob must reject uid-alice."""
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-bob")

        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "CART-1")
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["error"] == "cart_already_assigned"

    def test_customer_already_owns_other_cart_rejected(self):
        """A customer with an active session elsewhere cannot pair to a new cart."""
        sid1 = create_session(cart_id="CART-1")
        assign_customer_to_session(sid1, "uid-alice")

        create_session(cart_id="CART-2")

        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "CART-2")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "customer_already_has_active_session"

    def test_nonexistent_cart_id_rejected(self):
        """An ineligible physical cart must return 404 without creation."""
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "CART-NONEXISTENT")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "cart_not_allowed"

    def test_cart_without_active_session_created(self):
        """A cart_id that was never used has no active session."""
        # No session created — cart_id has never been used.
        sync = _CapturingSync()
        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-UNUSED",
            _firebase_sync_fn=sync,
        )
        assert result.status == "paired"
        assert get_session_meta(result.session_id)["customer_uid"] == "uid-alice"

    def test_completed_session_not_considered_active(self):
        """A completed session (end_time IS NOT NULL) must not be paired."""
        sid = create_session(cart_id="CART-1")
        complete_session(sid)  # sets end_time

        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "CART-1")
        assert resp.status_code == 200
        assert resp.get_json()["session_id"] != sid
        assert get_session_meta(sid)["end_time"] is not None

    def test_multiple_active_sessions_for_cart_rejected(self, db_conn, isolated_db, monkeypatch):
        """Two active sessions for the same cart_id must produce a conflict."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        # Directly insert two active sessions with the same cart_id.
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id) "
            "VALUES (datetime('now'), 0, 'CART-DUP')"
        )
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id) "
            "VALUES (datetime('now'), 0, 'CART-DUP')"
        )
        db_conn.commit()

        with pytest.raises(IntegrityConflict) as exc_info:
            get_active_session_for_cart("CART-DUP", _conn=db_conn)
        assert exc_info.value.code == "multiple_active_sessions_for_cart"

    def test_multiple_active_sessions_for_cart_via_http(self, db_conn, isolated_db, monkeypatch):
        """HTTP endpoint returns 409 when multiple active sessions exist for cart."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id) "
            "VALUES (datetime('now'), 0, 'CART-DUP')"
        )
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id) "
            "VALUES (datetime('now'), 0, 'CART-DUP')"
        )
        db_conn.commit()

        client = _make_client("uid-alice")
        resp = _pair(client, "CART-DUP")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "multiple_active_sessions_for_cart"

    def test_multiple_active_sessions_for_customer_rejected(self, db_conn, isolated_db, monkeypatch):
        """Two active sessions for the same customer_uid must produce a conflict."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id, customer_uid) "
            "VALUES (datetime('now'), 0, 'CART-A', 'uid-dup')"
        )
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id, customer_uid) "
            "VALUES (datetime('now'), 0, 'CART-B', 'uid-dup')"
        )
        db_conn.commit()

        with pytest.raises(IntegrityConflict) as exc_info:
            get_active_session_for_customer("uid-dup", _conn=db_conn)
        assert exc_info.value.code == "multiple_active_sessions_for_customer"

    def test_empty_cart_id_rejected(self):
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_cart_id"

    def test_whitespace_only_cart_id_rejected(self):
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "   ")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_cart_id"

    def test_missing_cart_id_in_body_rejected(self):
        sync = _CapturingSync()
        app = create_app(
            token_verifier=_verifier_ok("uid-alice"),
            firebase_sync_fn=sync,
        )
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.post(
            "/pair-cart",
            json={},
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_cart_id"

    def test_non_json_body_rejected(self):
        sync = _CapturingSync()
        app = create_app(
            token_verifier=_verifier_ok("uid-alice"),
            firebase_sync_fn=sync,
        )
        app.config["TESTING"] = True
        client = app.test_client()
        resp = client.post(
            "/pair-cart",
            data="not-json",
            content_type="text/plain",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_request"


# ===========================================================================
# Persistence tests
# ===========================================================================

class TestPersistence:

    def test_successful_assignment_persists_to_sqlite(self):
        """After pairing, customer_uid must be in SQLite sessions row."""
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        create_session(cart_id="CART-1")

        resp = _pair(client, "CART-1")
        assert resp.status_code == 200

        sid = resp.get_json()["session_id"]
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-alice"

    def test_customer_uid_is_correct_in_sqlite(self):
        """The persisted customer_uid must match the token UID, not any other value."""
        sync = _CapturingSync()
        client = _make_client("uid-correct", sync)
        create_session(cart_id="CART-1")

        _pair(client, "CART-1")
        sid = get_session_meta  # just verify via pairing result
        resp = _pair(client, "CART-1")  # idempotent second call
        session_id = resp.get_json()["session_id"]
        meta = get_session_meta(session_id)
        assert meta["customer_uid"] == "uid-correct"

    def test_firebase_sync_receives_correct_uid(self):
        """The Firebase sync function receives the verified UID, not a fake one."""
        sync = _CapturingSync()
        create_session(cart_id="CART-1")

        pair_customer_to_cart(
            customer_uid="uid-verified",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert len(sync.calls) == 1
        _, synced_uid, synced_cart = sync.calls[0]
        assert synced_uid == "uid-verified"
        assert synced_cart == "CART-1"

    def test_firebase_sync_receives_correct_session_id(self):
        """Firebase sync receives the same session_id as SQLite."""
        sync = _CapturingSync()
        sid = create_session(cart_id="CART-1")

        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert sync.calls[0][0] == sid
        assert result.session_id == sid

    def test_firebase_sync_failure_does_not_erase_sqlite_ownership(self):
        """
        When Firebase sync raises, the SQLite customer_uid must still be set.
        SQLite is authoritative; Firebase is a downstream sync layer.
        """
        sync = _CapturingSync()
        sync.raise_on_next = RuntimeError("simulated Firebase failure")

        sid = create_session(cart_id="CART-1")

        # Sync will fail, but pairing logic still returns success.
        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        # The result is still "paired" because SQLite succeeded.
        assert result.status == "paired"

        # SQLite must still have the uid.
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-alice"

    def test_completed_sessions_remain_intact_after_pairing_another(self):
        """Completing one session and pairing another must not affect the first."""
        sid1 = create_session(cart_id="CART-1")
        assign_customer_to_session(sid1, "uid-alice")
        complete_session(sid1)

        sid2 = create_session(cart_id="CART-2")
        sync = _CapturingSync()
        pair_customer_to_cart(
            customer_uid="uid-bob",
            cart_id="CART-2",
            _firebase_sync_fn=sync,
        )

        # History session intact.
        meta1 = get_session_meta(sid1)
        assert meta1["customer_uid"] == "uid-alice"
        assert meta1["end_time"] is not None

        # New session paired correctly.
        meta2 = get_session_meta(sid2)
        assert meta2["customer_uid"] == "uid-bob"
        assert meta2["end_time"] is None


# ===========================================================================
# Concurrency / atomic assignment tests
# ===========================================================================

class TestConcurrencyInvariants:

    def test_assign_customer_atomic_succeeds_when_unassigned(self, db_conn, isolated_db, monkeypatch):
        """assign_customer_atomic succeeds on an unassigned session."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount) "
            "VALUES (datetime('now'), 0)"
        )
        db_conn.commit()
        db_conn.execute("SELECT last_insert_rowid()")
        sid = db_conn.execute("SELECT max(session_id) FROM sessions").fetchone()[0]

        assign_customer_atomic(sid, "uid-first", _conn=db_conn)
        db_conn.commit()

        row = db_conn.execute(
            "SELECT customer_uid FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        assert row[0] == "uid-first"

    def test_assign_customer_atomic_idempotent_for_same_uid(self, db_conn, isolated_db, monkeypatch):
        """assign_customer_atomic is idempotent when same UID is already set."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, customer_uid) "
            "VALUES (datetime('now'), 0, 'uid-alice')"
        )
        db_conn.commit()
        sid = db_conn.execute("SELECT max(session_id) FROM sessions").fetchone()[0]

        # Should not raise.
        assign_customer_atomic(sid, "uid-alice", _conn=db_conn)

    def test_assign_customer_atomic_raises_if_different_uid_present(self, db_conn, isolated_db, monkeypatch):
        """assign_customer_atomic detects a concurrent race and raises IntegrityConflict."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, customer_uid) "
            "VALUES (datetime('now'), 0, 'uid-winner')"
        )
        db_conn.commit()
        sid = db_conn.execute("SELECT max(session_id) FROM sessions").fetchone()[0]

        with pytest.raises(IntegrityConflict) as exc_info:
            assign_customer_atomic(sid, "uid-loser", _conn=db_conn)
        assert exc_info.value.code == "cart_already_assigned"

    def test_pair_cart_logic_rejects_on_atomic_race(self, monkeypatch):
        """
        When pair_and_assign_in_transaction raises IntegrityConflict, pair_customer_to_cart
        surfaces it as a PairingError with code=cart_already_assigned.
        """
        sid = create_session(cart_id="CART-1")

        # Simulate a conflict from the protected transaction.
        from edge.pairing import pair_cart as _pc_module

        def _race_assign(session_id, uid, **kwargs):
            raise IntegrityConflict(
                code="cart_already_assigned",
                message="Concurrent race detected.",
            )

        monkeypatch.setattr(_pc_module, "pair_and_assign_in_transaction", _race_assign)

        with pytest.raises(PairingError) as exc_info:
            pair_customer_to_cart(
                customer_uid="uid-alice",
                cart_id="CART-1",
                _firebase_sync_fn=_CapturingSync(),
            )
        assert exc_info.value.code == "cart_already_assigned"
        assert exc_info.value.http_status == 409


# ===========================================================================
# Session store unit tests
# ===========================================================================

class TestSessionStore:

    def test_get_active_session_for_cart_returns_none_when_absent(self):
        result = get_active_session_for_cart("CART-NONEXISTENT")
        assert result is None

    def test_get_active_session_for_cart_returns_active(self):
        sid = create_session(cart_id="CART-A")
        result = get_active_session_for_cart("CART-A")
        assert result is not None
        assert result["session_id"] == sid
        assert result["cart_id"] == "CART-A"
        assert result["end_time"] is None

    def test_get_active_session_for_cart_ignores_completed(self):
        sid = create_session(cart_id="CART-A")
        complete_session(sid)
        result = get_active_session_for_cart("CART-A")
        assert result is None

    def test_get_active_session_for_customer_returns_none_when_absent(self):
        result = get_active_session_for_customer("uid-nobody")
        assert result is None

    def test_get_active_session_for_customer_returns_active(self):
        sid = create_session(cart_id="CART-A")
        assign_customer_to_session(sid, "uid-alice")
        result = get_active_session_for_customer("uid-alice")
        assert result is not None
        assert result["session_id"] == sid
        assert result["customer_uid"] == "uid-alice"

    def test_get_active_session_for_customer_ignores_completed(self):
        sid = create_session(cart_id="CART-A")
        assign_customer_to_session(sid, "uid-alice")
        complete_session(sid)
        result = get_active_session_for_customer("uid-alice")
        assert result is None


# ===========================================================================
# Response contract tests
# ===========================================================================

class TestResponseContract:

    def test_success_response_has_required_fields(self):
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        create_session(cart_id="CART-1")

        resp = _pair(client, "CART-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "session_id" in data
        assert "cart_id" in data
        assert "status" in data
        # Must NOT contain sensitive fields.
        assert "customer_uid" not in data
        assert "token" not in data
        assert "error" not in data

    def test_error_response_has_error_and_message(self):
        sync = _CapturingSync()
        client = _make_client("uid-alice", sync)
        resp = _pair(client, "CART-NONEXISTENT")
        assert resp.status_code == 404
        data = resp.get_json()
        assert "error" in data
        assert "message" in data
        # Must not expose stack traces or internals.
        assert "Traceback" not in data.get("message", "")
        assert "sqlite" not in data.get("message", "").lower()

    def test_error_does_not_expose_credentials(self):
        verifier = _verifier_error(ValueError("secret-cred-string"))
        client = _make_client_with_verifier(verifier)
        resp = _pair(client, "CART-1")
        raw = resp.get_data(as_text=True)
        assert "secret-cred-string" not in raw
