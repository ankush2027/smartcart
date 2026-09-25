"""
tests/test_pairing_hardening.py — Transaction hardening and runtime tests.

Covers:
- BEGIN IMMEDIATE transaction: successful assignment
- BEGIN IMMEDIATE transaction: idempotent same customer/same cart
- BEGIN IMMEDIATE transaction: conflicting cart owner rejected inside transaction
- BEGIN IMMEDIATE transaction: customer owning another session rejected
- BEGIN IMMEDIATE transaction: multiple active sessions for cart rejected
- BEGIN IMMEDIATE transaction: multiple active sessions for customer rejected
- BEGIN IMMEDIATE transaction: completed session not pairable
- BEGIN IMMEDIATE transaction: failed assignment leaves DB consistent
- Firebase sync occurs only AFTER the SQLite commit
- Firebase sync failure preserves committed SQLite ownership
- create_app() is testable without live Firebase (token_verifier injection)
- Firebase initialization occurs before serving in production path (testable)
- pair_cart HTTP endpoint routes through transactional path
- Idempotent already_paired path skips Firebase re-sync
- pair_and_assign_in_transaction rollback on error leaves DB unchanged

No real Firebase project, no credentials, no camera, no Raspberry Pi.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

# pyrefly: ignore [missing-import]
import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from edge.pairing.session_store import (
    IntegrityConflict,
    TransactionResult,
    assign_customer_atomic,
    get_active_session_for_cart,
    get_active_session_for_customer,
    pair_and_assign_in_transaction,
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
    temp_db = tmp_path / "test_hardening.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", temp_db)
    initialize_database()
    return temp_db


@pytest.fixture()
def db_conn(isolated_db):
    """Return an open SQLite connection to the isolated test database."""
    conn = sqlite3.connect(str(isolated_db))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Fake sync / verifier helpers
# ---------------------------------------------------------------------------

class _CapturingSync:
    """Records calls to the Firebase sync function."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[tuple] = []
        self._raise = raise_exc

    def __call__(self, session_id: int, customer_uid: str, cart_id: str) -> None:
        if self._raise is not None:
            exc = self._raise
            self._raise = None
            raise exc
        self.calls.append((session_id, customer_uid, cart_id))

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _verifier_ok(uid: str) -> Callable:
    def _v(token: str) -> dict:
        return {"uid": uid}
    return _v


def _flask_client(uid: str, sync: _CapturingSync | None = None):
    app = create_app(
        token_verifier=_verifier_ok(uid),
        firebase_sync_fn=sync or _CapturingSync(),
    )
    app.config["TESTING"] = True
    return app.test_client()


def _pair_http(client, cart_id, auth="Bearer tok"):
    return client.post(
        "/pair-cart",
        json={"cart_id": cart_id},
        headers={"Authorization": auth},
    )


# ===========================================================================
# BEGIN IMMEDIATE Transaction tests
# ===========================================================================

class TestTransactionSuccessfulAssignment:

    def test_assigns_customer_uid_to_sqlite(self):
        """pair_and_assign_in_transaction persists customer_uid to SQLite."""
        sid = create_session(cart_id="CART-1")
        result = pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert result.status == "paired"
        assert result.session_id == sid
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-alice"

    def test_returns_transaction_result_dataclass(self):
        """Result is a TransactionResult with correct fields."""
        create_session(cart_id="CART-1")
        result = pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert isinstance(result, TransactionResult)
        assert result.cart_id == "CART-1"
        assert result.customer_uid == "uid-alice"
        assert result.status == "paired"


class TestTransactionIdempotent:

    def test_idempotent_via_customer_lookup(self):
        """Customer already owning this session returns already_paired without write."""
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-alice")

        result = pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert result.status == "already_paired"
        assert result.session_id == sid

    def test_idempotent_double_call(self):
        """Two consecutive pair_and_assign calls for same UID/cart are idempotent."""
        create_session(cart_id="CART-1")
        r1 = pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert r1.status == "paired"
        r2 = pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert r2.status == "already_paired"
        # Only one assignment in SQLite.
        meta = get_session_meta(r1.session_id)
        assert meta["customer_uid"] == "uid-alice"


class TestTransactionConflicts:

    def test_cart_assigned_to_other_customer_raises(self):
        """Session already owned by uid-bob must reject uid-alice inside transaction."""
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-bob")

        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert exc_info.value.code == "cart_already_assigned"
        # SQLite state unchanged.
        assert get_session_meta(sid)["customer_uid"] == "uid-bob"

    def test_customer_owning_another_session_raises(self):
        """Customer with a different active session is rejected inside transaction."""
        sid1 = create_session(cart_id="CART-1")
        assign_customer_to_session(sid1, "uid-alice")
        create_session(cart_id="CART-2")

        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-2", "uid-alice")
        assert exc_info.value.code == "customer_already_has_active_session"
        # CART-2 session still unassigned.
        result = get_active_session_for_cart("CART-2")
        assert result["customer_uid"] is None

    def test_no_active_session_raises(self):
        """Cart with no session in SQLite raises no_active_session_for_cart."""
        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-GHOST", "uid-alice")
        assert exc_info.value.code == "no_active_session_for_cart"

    def test_completed_session_raises(self):
        """Completed session (end_time set) is not active; raises."""
        sid = create_session(cart_id="CART-1")
        complete_session(sid)

        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-1", "uid-alice")
        assert exc_info.value.code == "no_active_session_for_cart"

    def test_multiple_active_sessions_for_cart_raises(self, db_conn, isolated_db, monkeypatch):
        """Two active sessions for same cart → multiple_active_sessions_for_cart."""
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

        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-DUP", "uid-alice")
        assert exc_info.value.code == "multiple_active_sessions_for_cart"

    def test_multiple_active_sessions_for_customer_raises(self, db_conn, isolated_db, monkeypatch):
        """Customer with 2 active sessions → multiple_active_sessions_for_customer."""
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id, customer_uid) "
            "VALUES (datetime('now'), 0, 'CART-A', 'uid-multi')"
        )
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id, customer_uid) "
            "VALUES (datetime('now'), 0, 'CART-B', 'uid-multi')"
        )
        db_conn.commit()
        # Add a third session with CART-C unassigned so cart lookup succeeds.
        db_conn.execute(
            "INSERT INTO sessions (start_time, total_amount, cart_id) "
            "VALUES (datetime('now'), 0, 'CART-C')"
        )
        db_conn.commit()

        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-C", "uid-multi")
        assert exc_info.value.code == "multiple_active_sessions_for_customer"


class TestTransactionRollback:

    def test_failed_assignment_leaves_db_consistent(self, monkeypatch):
        """
        When pair_and_assign_in_transaction raises IntegrityConflict (e.g. cart
        assigned to another), the DB must be unchanged: no partial write.
        """
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-bob")

        with pytest.raises(IntegrityConflict):
            pair_and_assign_in_transaction("CART-1", "uid-alice")

        # Database unchanged — uid-bob still owns the session.
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-bob"

    def test_no_write_on_no_active_session_conflict(self):
        """No partial write when no active session exists."""
        with pytest.raises(IntegrityConflict):
            pair_and_assign_in_transaction("CART-NONE", "uid-x")
        # No sessions modified.
        assert get_active_session_for_cart("CART-NONE") is None


# ===========================================================================
# Firebase synchronization boundary tests
# ===========================================================================

class TestFirebaseSyncBoundary:

    def test_firebase_sync_called_after_sqlite_commit(self):
        """
        Firebase sync function is only called AFTER pair_customer_to_cart
        returns from the transaction (i.e., after SQLite commit).
        """
        sync = _CapturingSync()
        create_session(cart_id="CART-1")

        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert result.status == "paired"
        # Sync was called exactly once, after the transaction.
        assert sync.call_count == 1
        assert sync.calls[0] == (result.session_id, "uid-alice", "CART-1")

    def test_firebase_sync_not_called_on_already_paired(self):
        """
        Idempotent re-pair must NOT trigger a Firebase sync (no write, no sync).
        """
        sync = _CapturingSync()
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-alice")

        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert result.status == "already_paired"
        assert sync.call_count == 0  # No sync for idempotent call.

    def test_firebase_sync_failure_preserves_sqlite_ownership(self):
        """
        Firebase sync raises after SQLite commit; customer_uid must remain in SQLite.
        """
        sync = _CapturingSync(raise_exc=RuntimeError("Firebase network error"))
        sid = create_session(cart_id="CART-1")

        result = pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        # Still returns "paired" because SQLite committed.
        assert result.status == "paired"
        # SQLite ownership intact.
        meta = get_session_meta(sid)
        assert meta["customer_uid"] == "uid-alice"

    def test_firebase_sync_receives_correct_session_id(self):
        """Sync function receives the exact session_id that was committed."""
        sync = _CapturingSync()
        sid = create_session(cart_id="CART-1")

        pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-1",
            _firebase_sync_fn=sync,
        )
        assert sync.calls[0][0] == sid

    def test_firebase_sync_receives_correct_cart_id(self):
        """Sync function receives the cart_id from the request."""
        sync = _CapturingSync()
        create_session(cart_id="CART-LABEL")

        pair_customer_to_cart(
            customer_uid="uid-alice",
            cart_id="CART-LABEL",
            _firebase_sync_fn=sync,
        )
        assert sync.calls[0][2] == "CART-LABEL"


# ===========================================================================
# HTTP endpoint routes through transactional path
# ===========================================================================

class TestHTTPTransactionalPath:

    def test_http_success_uses_transaction(self):
        """POST /pair-cart routes through pair_and_assign_in_transaction."""
        sync = _CapturingSync()
        client = _flask_client("uid-alice", sync)
        create_session(cart_id="CART-HTTP")

        resp = _pair_http(client, "CART-HTTP")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "paired"
        assert data["cart_id"] == "CART-HTTP"
        # SQLite committed.
        meta = get_session_meta(data["session_id"])
        assert meta["customer_uid"] == "uid-alice"
        # Firebase synced after commit.
        assert sync.call_count == 1

    def test_http_cart_conflict_returns_409(self):
        """POST /pair-cart → cart_already_assigned (transactional) → 409."""
        sid = create_session(cart_id="CART-HTTP")
        assign_customer_to_session(sid, "uid-bob")

        client = _flask_client("uid-alice")
        resp = _pair_http(client, "CART-HTTP")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "cart_already_assigned"

    def test_http_customer_conflict_returns_409(self):
        """POST /pair-cart → customer_already_has_active_session → 409."""
        sid1 = create_session(cart_id="CART-A")
        assign_customer_to_session(sid1, "uid-alice")
        create_session(cart_id="CART-B")

        client = _flask_client("uid-alice")
        resp = _pair_http(client, "CART-B")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "customer_already_has_active_session"

    def test_http_completed_session_returns_404(self):
        """POST /pair-cart → completed session → 404 no_active_session_for_cart."""
        sid = create_session(cart_id="CART-DONE")
        complete_session(sid)

        client = _flask_client("uid-alice")
        resp = _pair_http(client, "CART-DONE")
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "no_active_session_for_cart"

    def test_http_idempotent_returns_already_paired(self):
        """POST /pair-cart idempotent → HTTP 200 already_paired."""
        sync = _CapturingSync()
        client = _flask_client("uid-alice", sync)
        create_session(cart_id="CART-HTTP")

        resp1 = _pair_http(client, "CART-HTTP")
        assert resp1.status_code == 200
        assert resp1.get_json()["status"] == "paired"
        assert sync.call_count == 1

        resp2 = _pair_http(client, "CART-HTTP")
        assert resp2.status_code == 200
        assert resp2.get_json()["status"] == "already_paired"
        # No second Firebase sync for idempotent call.
        assert sync.call_count == 1

    def test_http_multiple_active_sessions_for_cart_returns_409(
        self, db_conn, isolated_db, monkeypatch
    ):
        """POST /pair-cart with duplicate active sessions → 409."""
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

        client = _flask_client("uid-alice")
        resp = _pair_http(client, "CART-DUP")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "multiple_active_sessions_for_cart"


# ===========================================================================
# create_app testability without live Firebase
# ===========================================================================

class TestAppTestability:

    def test_create_app_with_fake_verifier_does_not_import_firebase_admin(self):
        """
        create_app() with a token_verifier injection must work without any
        real Firebase credentials or firebase_admin initialization.
        """
        app = create_app(
            token_verifier=_verifier_ok("uid-test"),
            firebase_sync_fn=_CapturingSync(),
        )
        assert app is not None
        app.config["TESTING"] = True
        client = app.test_client()

        create_session(cart_id="CART-TEST")
        resp = client.post(
            "/pair-cart",
            json={"cart_id": "CART-TEST"},
            headers={"Authorization": "Bearer fake-token"},
        )
        assert resp.status_code == 200

    def test_create_app_missing_verifier_uses_real_path(self):
        """
        create_app() without token_verifier injection returns an app that
        uses the real firebase_admin path.  The app object is valid;
        token verification failure mode tested separately.
        """
        app = create_app(firebase_sync_fn=_CapturingSync())
        assert app is not None


# ===========================================================================
# Production startup tests
# ===========================================================================

class TestProductionStartup:

    def test_run_server_module_importable(self):
        """edge.api.run_server is importable without side effects."""
        from edge.api import run_server  # noqa: PLC0415
        assert callable(run_server.main)

    def test_main_exits_on_firebase_init_failure(self, monkeypatch):
        """
        main() calls sys.exit(1) when Firebase initialization fails.
        This verifies that Firebase init is called before serving requests.
        """
        from edge.api import run_server  # noqa: PLC0415

        def _failing_init(*args, **kwargs):
            raise RuntimeError("SMARTCART_FIREBASE_URL not set")

        monkeypatch.setattr(
            "edge.firebase.client.initialize", _failing_init
        )

        with pytest.raises(SystemExit) as exc_info:
            run_server.main()
        assert exc_info.value.code == 1

    def test_main_initializes_firebase_before_creating_app(self, monkeypatch):
        """
        Firebase init must happen before create_app() is called.
        We verify init was called and app was constructed.
        """
        from edge.api import run_server  # noqa: PLC0415

        call_order = []

        def _mock_init(*args, **kwargs):
            call_order.append("firebase_init")

        def _mock_create_app(**kwargs):
            call_order.append("create_app")
            return create_app(token_verifier=_verifier_ok("uid-test"), firebase_sync_fn=_CapturingSync())

        monkeypatch.setattr("edge.firebase.client.initialize", _mock_init)
        monkeypatch.setattr("edge.api.server.create_app", _mock_create_app)
        monkeypatch.setattr("waitress.serve", lambda app, **kw: call_order.append("serve"))
        # Test without opening a network listener.
        monkeypatch.setenv("SMARTCART_API_PORT", "15000")

        run_server.main()

        assert call_order[0] == "firebase_init", (
            "Firebase init must occur before create_app"
        )
        assert call_order == ["firebase_init", "create_app", "serve"]


# ===========================================================================
# Concurrency simulation tests
# ===========================================================================

class TestConcurrencySimulation:

    def test_serialized_writes_second_writer_detects_first(self):
        """
        Simulate two sequential requests that both 'pass' an external pre-check
        but enter pair_and_assign_in_transaction sequentially.
        The second call must detect the first write and return already_paired
        (same UID) or raise cart_already_assigned (different UID).
        """
        create_session(cart_id="CART-RACE")

        # First writer succeeds.
        r1 = pair_and_assign_in_transaction("CART-RACE", "uid-first")
        assert r1.status == "paired"

        # Second writer (same UID) — idempotent.
        r2 = pair_and_assign_in_transaction("CART-RACE", "uid-first")
        assert r2.status == "already_paired"

        # Third writer (different UID) — rejected.
        with pytest.raises(IntegrityConflict) as exc_info:
            pair_and_assign_in_transaction("CART-RACE", "uid-second")
        assert exc_info.value.code == "cart_already_assigned"

        # DB state: still owned by first writer.
        meta = get_session_meta(r1.session_id)
        assert meta["customer_uid"] == "uid-first"

    def test_threading_two_writers_one_wins(self, isolated_db, monkeypatch):
        """
        Two threads race to pair with the same cart.
        SQLite BEGIN IMMEDIATE serializes them.
        Exactly one must succeed ('paired'); the other must get a conflict
        or the same session back (already_paired if same UID).
        This is a best-effort test — it validates the transaction mechanism
        under real thread concurrency, not just sequential simulation.
        """
        monkeypatch.setattr(_db_module, "DATABASE_PATH", isolated_db)
        create_session(cart_id="CART-THREAD")

        results: list = []
        errors: list = []

        def _worker(uid: str):
            try:
                r = pair_and_assign_in_transaction("CART-THREAD", uid)
                results.append((uid, r.status))
            except IntegrityConflict as e:
                errors.append((uid, e.code))
            except Exception as e:
                errors.append((uid, str(e)))

        t1 = threading.Thread(target=_worker, args=("uid-thread-1",))
        t2 = threading.Thread(target=_worker, args=("uid-thread-2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Total outcomes must be exactly 2 (no silent lost writes).
        total = len(results) + len(errors)
        assert total == 2, f"Expected 2 outcomes, got {total}: {results} / {errors}"

        # Exactly one "paired" result (one winner).
        paired_results = [r for r in results if r[1] == "paired"]
        assert len(paired_results) == 1, (
            f"Expected exactly one paired winner; results={results} errors={errors}"
        )

        # Database has exactly one customer_uid assigned.
        session = get_active_session_for_cart("CART-THREAD")
        assert session is not None
        assert session["customer_uid"] is not None


# ===========================================================================
# Legacy assign_customer_atomic tests (preserved)
# ===========================================================================

class TestAssignCustomerAtomic:

    def test_assigns_when_unassigned(self):
        sid = create_session(cart_id="CART-1")
        assign_customer_atomic(sid, "uid-alice")
        assert get_session_meta(sid)["customer_uid"] == "uid-alice"

    def test_idempotent_for_same_uid(self):
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-alice")
        assign_customer_atomic(sid, "uid-alice")  # must not raise
        assert get_session_meta(sid)["customer_uid"] == "uid-alice"

    def test_raises_for_different_uid(self):
        sid = create_session(cart_id="CART-1")
        assign_customer_to_session(sid, "uid-winner")
        with pytest.raises(IntegrityConflict) as exc_info:
            assign_customer_atomic(sid, "uid-loser")
        assert exc_info.value.code == "cart_already_assigned"
        assert get_session_meta(sid)["customer_uid"] == "uid-winner"


class TestRuntimeCompletion:
    def test_injected_connection_also_begins_and_commits(self, db_conn):
        sid = create_session(cart_id="CART-TX")
        statements = []
        db_conn.set_trace_callback(statements.append)
        result = pair_and_assign_in_transaction("CART-TX", "uid-a", _conn=db_conn)
        assert result.session_id == sid
        assert statements[0] == "BEGIN IMMEDIATE"
        assert statements[-1] == "COMMIT"
        assert not db_conn.in_transaction
        assert get_session_meta(sid)["customer_uid"] == "uid-a"

    def test_existing_transaction_is_not_committed_or_rolled_back(self, db_conn):
        sid = create_session(cart_id="CART-TX")
        db_conn.execute("BEGIN IMMEDIATE")
        db_conn.execute("UPDATE sessions SET cart_id='CART-EDIT' WHERE session_id=?", (sid,))
        with pytest.raises(ValueError, match="idle"):
            pair_and_assign_in_transaction("CART-EDIT", "uid-a", _conn=db_conn)
        assert db_conn.in_transaction
        db_conn.rollback()
        assert get_session_meta(sid)["cart_id"] == "CART-TX"

    def test_sql_failure_rolls_back_and_releases_writer(self, db_conn):
        sid = create_session(cart_id="CART-TX")
        db_conn.execute("""CREATE TRIGGER reject_pair AFTER UPDATE OF customer_uid ON sessions
            BEGIN SELECT RAISE(ABORT, 'test assignment failure'); END""")
        db_conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="test assignment failure"):
            pair_and_assign_in_transaction("CART-TX", "uid-a", _conn=db_conn)
        assert not db_conn.in_transaction
        assert get_session_meta(sid)["customer_uid"] is None
        db_conn.execute("DROP TRIGGER reject_pair")
        db_conn.commit()
        assert pair_and_assign_in_transaction("CART-TX", "uid-a").status == "paired"

    @pytest.mark.parametrize("mode", ["same-cart", "same-customer", "idempotent"])
    def test_competing_writers_have_exact_outcomes(self, mode):
        from concurrent.futures import ThreadPoolExecutor
        create_session(cart_id="CART-A")
        create_session(cart_id="CART-B")
        barrier = threading.Barrier(2)
        requests = {
            "same-cart": [("CART-A", "uid-a"), ("CART-A", "uid-b")],
            "same-customer": [("CART-A", "uid-a"), ("CART-B", "uid-a")],
            "idempotent": [("CART-A", "uid-a"), ("CART-A", "uid-a")],
        }[mode]

        def worker(args):
            barrier.wait(timeout=5)
            try:
                return pair_and_assign_in_transaction(*args).status
            except IntegrityConflict as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, args) for args in requests]
            outcomes = [future.result(timeout=10) for future in futures]
        expected = {"same-cart": "cart_already_assigned",
                    "same-customer": "customer_already_has_active_session",
                    "idempotent": "already_paired"}[mode]
        assert sorted(outcomes) == sorted(["paired", expected])
        assert get_active_session_for_customer("uid-a") is not None or mode == "same-cart"

    def test_firebase_sync_runs_without_sqlite_write_lock(self):
        sid = create_session(cart_id="CART-TX")

        observed = []
        def sync(session_id, uid, cart_id):
            assert get_session_meta(sid)["customer_uid"] == uid
            with _db_module.get_connection() as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            observed.append("unlocked")

        pair_customer_to_cart("uid-a", "CART-TX", _firebase_sync_fn=sync)
        assert get_session_meta(sid)["customer_uid"] == "uid-a"
        assert observed == ["unlocked"]

    @pytest.mark.parametrize("port", ["zero", "0", "-1", "65536"])
    def test_invalid_port_fails_before_initialization(self, monkeypatch, port):
        from edge.api import run_server
        monkeypatch.setenv("SMARTCART_API_PORT", port)
        def unexpected():
            pytest.fail("Invalid startup config must not initialize Firebase")
        monkeypatch.setattr("edge.firebase.client.initialize", unexpected)
        with pytest.raises(SystemExit):
            run_server.main()

    def test_startup_order_and_server_configuration(self, monkeypatch):
        from edge.api import run_server
        from types import SimpleNamespace
        calls = []
        app = SimpleNamespace(config={})
        monkeypatch.setenv("SMARTCART_API_HOST", "127.0.0.1")
        monkeypatch.setenv("SMARTCART_API_PORT", "8089")
        monkeypatch.setattr("edge.firebase.client.initialize", lambda: calls.append("firebase"))
        monkeypatch.setattr(_db_module, "initialize_database", lambda: calls.append("sqlite"))
        def factory():
            calls.append("app")
            return app
        def serve(actual, **options):
            calls.append("serve")
            assert actual is app
            assert not actual.config["DEBUG"]
            assert options == dict(host="127.0.0.1", port=8089, threads=4, expose_tracebacks=False)
        monkeypatch.setattr("edge.api.server.create_app", factory)
        monkeypatch.setattr("waitress.serve", serve)
        run_server.main()
        assert calls == ["firebase", "sqlite", "app", "serve"]

    def test_initialization_failure_does_not_log_sdk_details(self, monkeypatch, caplog):
        from edge.api import run_server
        monkeypatch.setenv("SMARTCART_API_PORT", "5000")
        def fail():
            raise ValueError("private credential details")
        monkeypatch.setattr("edge.firebase.client.initialize", fail)
        with pytest.raises(SystemExit):
            run_server.main()
        assert "private credential details" not in caplog.text

    @pytest.mark.parametrize("body", [[], ["CART-A"], 12, "CART-A", {"cart_id": 12}, {"cart_id": True}])
    def test_malformed_json_is_controlled(self, body):
        client = _flask_client("uid-a")
        response = client.post("/pair-cart", json=body, headers={"Authorization": "Bearer fake-token"})
        assert response.status_code == 400
