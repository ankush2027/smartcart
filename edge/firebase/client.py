"""
edge/firebase/client.py — Firebase Admin SDK initialization for SmartCart.

Responsibilities:
    - Read SMARTCART_FIREBASE_URL from the environment.
    - Use GOOGLE_APPLICATION_CREDENTIALS for service-account credentials.
    - Initialize the Firebase Admin SDK app exactly once.
    - Return a firebase_admin.db.Reference via get_reference().

Non-responsibilities:
    - Does NOT push session data, bill items, or event log entries.
    - Does NOT implement offline queues or retry logic.
    - Does NOT read from Firebase.
    - Does NOT know anything about the SQLite schema or approved Firebase tree.
    - Does NOT connect to Firebase during module import.

Lazy import
-----------
``firebase_admin`` is imported inside :func:`initialize` only — never at
module level.  This keeps module-level imports fast and allows the test
suite to import this module without ``firebase_admin`` being installed or
the SDK connecting to Firebase.  The same lazy-import principle is already
used by :mod:`edge.model.yolo_detector` for ``ultralytics``.

Testing
-------
Pass ``_fake_ref`` to :func:`initialize` to skip real Firebase
initialization entirely.  Use ``monkeypatch.setattr`` on the module-level
``_initialized`` and ``_injected_ref`` variables to reset state between
tests — the same approach used by ``test_cart_session`` for
``DATABASE_PATH``.
"""

from __future__ import annotations

import os


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

#: True once :func:`initialize` has been called successfully.
#: Monkeypatch this to ``False`` in tests to reset the module between cases.
_initialized: bool = False

#: Injected fake reference for testing.  When set, :func:`get_reference`
#: returns this object instead of calling ``firebase_admin.db.reference()``.
#: Monkeypatch this to ``None`` in tests alongside ``_initialized``.
_injected_ref = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def initialize(
    url: str | None = None,
    *,
    _fake_ref=None,
) -> None:
    """
    Initialize the Firebase Admin SDK for SmartCart.

    Parameters
    ----------
    url:
        Firebase Realtime Database URL.  When ``None`` (the default), the
        value is read from the ``SMARTCART_FIREBASE_URL`` environment
        variable.  Ignored when ``_fake_ref`` is supplied.
    _fake_ref:
        Injected fake reference object.  When supplied, all real Firebase
        initialization is skipped and ``firebase_admin`` is never imported.
        **For testing only.**

    Raises
    ------
    RuntimeError
        If ``SMARTCART_FIREBASE_URL`` is not set and no ``url`` is given.
    RuntimeError
        If ``GOOGLE_APPLICATION_CREDENTIALS`` is not set (real path only).

    Notes
    -----
    This function is idempotent: subsequent calls after a successful
    initialization are silently ignored, preserving the original state.

    Usage — production::

        initialize()          # reads env vars automatically
        ref = get_reference("/sessions/42")

    Usage — tests (no credentials required)::

        fake = _FakeReference()
        initialize(_fake_ref=fake)
        ref = get_reference("/sessions/42")   # returns fake
    """
    global _initialized, _injected_ref

    # Idempotency guard: subsequent calls after success are no-ops.
    if _initialized:
        return

    # ------------------------------------------------------------------
    # Testing path: fake reference injected → skip firebase_admin entirely.
    # ------------------------------------------------------------------
    if _fake_ref is not None:
        _injected_ref = _fake_ref
        _initialized = True
        return

    # ------------------------------------------------------------------
    # Real path: resolve configuration before touching the SDK so that
    # any missing-config errors are clear and specific.
    # ------------------------------------------------------------------
    if url is None:
        url = os.getenv("SMARTCART_FIREBASE_URL")

    if not url:
        raise RuntimeError(
            "Firebase Database URL is not configured. "
            "Set the SMARTCART_FIREBASE_URL environment variable to the "
            "Firebase Realtime Database URL "
            "(e.g. https://your-project-default-rtdb.firebaseio.com)."
        )

    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        raise RuntimeError(
            "Firebase credentials are not configured. "
            "Set the GOOGLE_APPLICATION_CREDENTIALS environment variable "
            "to the absolute path of the service-account JSON file "
            "(e.g. /home/pi/firebase-adminsdk-xxxx.json). "
            "The JSON file must NOT be committed to Git."
        )

    # Lazy import: only pay the firebase_admin cost when a real connection
    # is actually needed.  This keeps module-level imports fast and prevents
    # the test suite from requiring firebase_admin to be installed.
    # pyrefly: ignore [missing-import]
    import firebase_admin  # noqa: PLC0415
    # pyrefly: ignore [missing-import]
    from firebase_admin import credentials, db  # noqa: PLC0415

    # Guard against external callers who may have already called
    # firebase_admin.initialize_app() directly.
    try:
        firebase_admin.get_app()
    except ValueError:
        # App has not been initialized yet — initialize it now.
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {"databaseURL": url})

    _initialized = True


def get_reference(path: str = "/"):
    """
    Return a Firebase Realtime Database reference for ``path``.

    Parameters
    ----------
    path:
        Slash-separated path to the Firebase node, e.g.
        ``"/sessions/42/bill_items"``.  Defaults to the root ``"/"``.

    Returns
    -------
    firebase_admin.db.Reference
        A live database reference (or the injected fake reference when the
        client was initialized via :func:`initialize` with ``_fake_ref``).

    Raises
    ------
    RuntimeError
        If :func:`initialize` has not been called yet.

    Notes
    -----
    This function intentionally does NOT call :func:`initialize` itself.
    Callers are responsible for initializing the client before use.
    """
    if not _initialized:
        raise RuntimeError(
            "Firebase client is not initialized. "
            "Call edge.firebase.client.initialize() before get_reference()."
        )

    if _injected_ref is not None:
        return _injected_ref

    # Lazy import: firebase_admin is guaranteed to be imported already
    # (initialize() completed), but importing here avoids a module-level
    # import and is consistent with the lazy pattern above.
    # pyrefly: ignore [missing-import]
    from firebase_admin import db  # noqa: PLC0415

    return db.reference(path)
