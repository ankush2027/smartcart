"""
Tests for edge.firebase.client — Firebase Admin SDK initialization.

Design principles:
    - No real Firebase project required.
    - No service-account JSON required.
    - No internet access required.
    - firebase_admin is never imported by the test suite.
    - All tests are deterministic.

Fake-reference strategy:
    initialize() accepts an optional _fake_ref argument.
    When supplied, it skips firebase_admin entirely and stores the injected
    object as the database reference.

    _FakeReference mimics a firebase_admin.db.Reference but implements
    no real methods — identity checks are sufficient for client tests.
    (Real method fakes belong in test_firebase_sync.py, a future task.)

Isolation strategy:
    An autouse fixture resets the module-level _initialized and _injected_ref
    variables before every test via monkeypatch.setattr.  This is the same
    approach used by test_cart_session to redirect DATABASE_PATH to a temp
    SQLite file — no global state leaks between tests.

Covered cases:
     1.  get_reference() before initialization raises RuntimeError.
     2.  Missing SMARTCART_FIREBASE_URL produces a clear error.
     3.  Importing edge.firebase.client does not import firebase_admin.
     4.  Initialization state is clean (False) at the start of every test.
     5.  A fake reference is returned by get_reference() after fake init.
     6.  get_reference() with any path returns the stored fake reference.
     7.  Repeated initialize() calls are idempotent.
     8.  Tests require no real Firebase credentials (structural).
     9.  Missing GOOGLE_APPLICATION_CREDENTIALS produces a clear error.
    10.  _initialized is True after successful fake initialization.
"""

from __future__ import annotations

import importlib
import sys

# pyrefly: ignore [missing-import]
import pytest

import edge.firebase.client as _client_module
from edge.firebase.client import get_reference, initialize


# ---------------------------------------------------------------------------
# Fake reference
# ---------------------------------------------------------------------------

class _FakeReference:
    """
    Minimal stand-in for firebase_admin.db.Reference.

    No real methods are implemented for client initialization tests.
    Object identity (``is``) is the only assertion made about the return
    value of get_reference() in this test module.

    Method fakes (e.g. set(), push(), child()) belong in a future
    test_firebase_sync.py when the synchronization layer is tested.
    """


# ---------------------------------------------------------------------------
# Fixture: reset module-level state before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_client(monkeypatch):
    """
    Restore _initialized and _injected_ref to their default values
    before every test, regardless of outcome.

    Uses monkeypatch.setattr (the same approach as test_cart_session's
    isolated_db fixture which patches DATABASE_PATH) so that monkeypatch
    automatically undoes the patch on teardown.

    This guarantees that no state from one test bleeds into another,
    even when a test calls initialize() and sets _initialized = True.
    """
    monkeypatch.setattr(_client_module, "_initialized", False)
    monkeypatch.setattr(_client_module, "_injected_ref", None)


# ---------------------------------------------------------------------------
# Test 1 — get_reference() before initialization raises RuntimeError
# ---------------------------------------------------------------------------

def test_get_reference_before_init_raises():
    """
    Calling get_reference() without a prior initialize() must raise
    RuntimeError with a message that mentions 'initialized'.
    """
    with pytest.raises(RuntimeError, match="initialized"):
        get_reference()


def test_get_reference_with_path_before_init_raises():
    """
    get_reference() with an explicit path also raises before initialization.
    """
    with pytest.raises(RuntimeError, match="initialized"):
        get_reference("/sessions/1/bill_items")


# ---------------------------------------------------------------------------
# Test 2 — Missing SMARTCART_FIREBASE_URL produces a clear error
# ---------------------------------------------------------------------------

def test_missing_url_env_var_raises(monkeypatch):
    """
    initialize() with no URL argument and SMARTCART_FIREBASE_URL not set
    must raise RuntimeError with a message that names the variable.
    The error must be raised before firebase_admin is imported.
    """
    monkeypatch.delenv("SMARTCART_FIREBASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="SMARTCART_FIREBASE_URL"):
        initialize()


def test_empty_url_string_raises(monkeypatch):
    """
    initialize() with an explicitly empty URL string raises RuntimeError.
    An empty string is not a valid database URL and must be rejected.
    """
    monkeypatch.delenv("SMARTCART_FIREBASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="SMARTCART_FIREBASE_URL"):
        initialize(url="")


# ---------------------------------------------------------------------------
# Test 3 — Module import does not import firebase_admin
# ---------------------------------------------------------------------------

def test_module_import_does_not_import_firebase_admin():
    """
    Importing edge.firebase.client must not cause firebase_admin to be
    imported as a side effect.  firebase_admin must only be imported
    inside initialize() when a real (non-fake) connection is needed.

    This mirrors test_yolo_detector's check that 'ultralytics' is not
    imported at module-import time.
    """
    # Remove firebase_admin from sys.modules to guarantee a clean baseline.
    sys.modules.pop("firebase_admin", None)

    # Re-import the module to trigger module-level code again.
    importlib.import_module("edge.firebase.client")

    assert "firebase_admin" not in sys.modules, (
        "firebase_admin must not be imported at module load time. "
        "It must be lazy-imported inside initialize() only."
    )


# ---------------------------------------------------------------------------
# Test 4 — Initialization state is clean at the start of every test
# ---------------------------------------------------------------------------

def test_reset_fixture_gives_clean_state():
    """
    The autouse reset_client fixture must guarantee that _initialized is
    False and _injected_ref is None at the start of every test, regardless
    of what a previous test may have done.
    """
    assert _client_module._initialized is False
    assert _client_module._injected_ref is None


# ---------------------------------------------------------------------------
# Test 5 — Fake reference is returned after fake initialization
# ---------------------------------------------------------------------------

def test_fake_reference_returned_by_get_reference():
    """
    initialize(_fake_ref=fake) must store the fake so that get_reference()
    returns it without contacting Firebase or importing firebase_admin.
    """
    fake = _FakeReference()
    initialize(_fake_ref=fake)

    result = get_reference()
    assert result is fake


# ---------------------------------------------------------------------------
# Test 6 — Fake reference returned for any path
# ---------------------------------------------------------------------------

def test_fake_reference_returned_for_any_path():
    """
    get_reference() with any path string returns the stored fake reference
    when initialized via fake injection.  The client layer does not perform
    path-based routing — that is the responsibility of sync.py (future task).
    """
    fake = _FakeReference()
    initialize(_fake_ref=fake)

    assert get_reference("/") is fake
    assert get_reference("/sessions") is fake
    assert get_reference("/products") is fake
    assert get_reference("/sessions/42/bill_items/1") is fake
    assert get_reference("/sessions/42/event_log") is fake


# ---------------------------------------------------------------------------
# Test 7 — Repeated initialize() calls are idempotent
# ---------------------------------------------------------------------------

def test_repeated_initialize_does_not_replace_reference():
    """
    Calling initialize() more than once must be a no-op after the first
    successful call.  The original injected reference must be preserved
    and must not be replaced by the second call.
    """
    first_fake = _FakeReference()
    second_fake = _FakeReference()

    initialize(_fake_ref=first_fake)
    initialize(_fake_ref=second_fake)   # second call must be silently ignored

    result = get_reference()
    assert result is first_fake, (
        "Second initialize() call must not overwrite the original reference"
    )


def test_repeated_initialize_does_not_raise():
    """
    Calling initialize() multiple times must not raise any exception.
    """
    fake = _FakeReference()
    initialize(_fake_ref=fake)

    try:
        initialize(_fake_ref=fake)
        initialize(_fake_ref=fake)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Repeated initialize() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Test 8 — Tests require no real Firebase credentials (structural)
# ---------------------------------------------------------------------------

def test_no_credentials_required_for_fake_init(monkeypatch):
    """
    Fake initialization must succeed even when GOOGLE_APPLICATION_CREDENTIALS
    and SMARTCART_FIREBASE_URL are both absent from the environment.
    No real credentials are ever required for the test suite.
    """
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("SMARTCART_FIREBASE_URL", raising=False)

    fake = _FakeReference()
    # Must not raise regardless of environment variable state.
    initialize(_fake_ref=fake)

    assert _client_module._initialized is True
    assert get_reference() is fake


# ---------------------------------------------------------------------------
# Test 9 — Missing GOOGLE_APPLICATION_CREDENTIALS produces a clear error
# ---------------------------------------------------------------------------

def test_missing_credentials_env_var_raises(monkeypatch):
    """
    initialize() with a URL provided but GOOGLE_APPLICATION_CREDENTIALS not
    set must raise RuntimeError with a message that names the variable.
    The error must be raised before firebase_admin is imported.
    """
    monkeypatch.setenv("SMARTCART_FIREBASE_URL", "https://fake-project.firebaseio.com")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        initialize()


def test_missing_credentials_error_before_firebase_admin_import(monkeypatch):
    """
    When GOOGLE_APPLICATION_CREDENTIALS is missing, firebase_admin must
    not be imported.  The configuration check must happen first.
    """
    sys.modules.pop("firebase_admin", None)
    monkeypatch.setenv("SMARTCART_FIREBASE_URL", "https://fake-project.firebaseio.com")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        initialize()

    assert "firebase_admin" not in sys.modules


# ---------------------------------------------------------------------------
# Test 10 — _initialized is True after successful fake initialization
# ---------------------------------------------------------------------------

def test_initialized_flag_true_after_fake_init():
    """
    After a successful fake initialization, _initialized must be True
    so that get_reference() does not raise.
    """
    fake = _FakeReference()
    assert _client_module._initialized is False

    initialize(_fake_ref=fake)

    assert _client_module._initialized is True
