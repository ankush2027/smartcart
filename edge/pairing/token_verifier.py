"""
edge/pairing/token_verifier.py — Firebase ID-token verification for SmartCart.

Responsibilities
----------------
- Verify a Firebase ID token using the Firebase Admin SDK.
- Return the verified Firebase UID.
- Reject missing, malformed, expired, or invalid tokens.

Non-responsibilities
--------------------
- Does NOT initialize the Firebase Admin SDK (caller must call
  ``edge.firebase.client.initialize()`` before using the real verifier).
- Does NOT trust any UID supplied by the client request body.
- Does NOT decode JWTs without signature verification.
- Does NOT expose Admin SDK credentials.

Testability
-----------
``verify_id_token`` accepts an optional ``_verifier`` callable parameter.
In production this defaults to the Firebase Admin SDK's ``auth.verify_id_token``.
In tests, a fake callable is injected to avoid any real Firebase connection.

This is the single abstraction point for token verification; the pairing
logic and HTTP layer never import ``firebase_admin`` directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TokenVerificationError(Exception):
    """
    Raised when a Firebase ID token cannot be verified.

    The ``code`` attribute holds a machine-readable error string safe to
    return in an API response.  The ``message`` attribute holds a
    human-readable description that does NOT contain credentials or
    internal details.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

def verify_id_token(
    token: str,
    *,
    _verifier=None,
) -> str:
    """
    Verify *token* and return the authenticated Firebase UID.

    Parameters
    ----------
    token:
        A Firebase ID token string obtained from ``FirebaseAuth.getIdToken()``
        on the Flutter client.  Must be non-empty.
    _verifier:
        Optional callable with the signature ``(token: str) -> dict``.
        When supplied it is used instead of the real Firebase Admin SDK.
        **For testing only.**  In production, the real
        ``firebase_admin.auth.verify_id_token`` is used.

    Returns
    -------
    str
        The verified Firebase UID (``uid`` field of the decoded token).

    Raises
    ------
    TokenVerificationError
        ``code="authentication_required"``  — token is empty/None.
        ``code="invalid_token"``            — signature invalid, malformed,
                                              expired, or revoked.
        ``code="internal_error"``           — unexpected SDK failure.

    Notes
    -----
    The real Firebase Admin SDK path requires that the Firebase app has been
    initialized (``edge.firebase.client.initialize()`` must have been called).
    The ``_verifier`` injection bypasses this entirely for offline tests.
    """
    if not token:
        raise TokenVerificationError(
            code="authentication_required",
            message="Firebase ID token is required.",
        )

    if _verifier is None:
        # Lazy import: firebase_admin is only needed on the real path.
        # pyrefly: ignore [missing-import]
        from firebase_admin import auth  # noqa: PLC0415
        _verifier = auth.verify_id_token

    try:
        decoded = _verifier(token)
    except Exception as exc:
        # firebase_admin raises firebase_admin.auth.InvalidIdTokenError,
        # ExpiredIdTokenError, RevokedIdTokenError, CertificateFetchError, etc.
        # We catch all of them and surface a single controlled error code.
        # The original exception message is logged (server-side only) but never
        # forwarded to the client.
        exc_name = type(exc).__name__
        logger.warning(
            "Firebase token verification failed (%s): %s",
            exc_name,
            exc,
        )
        raise TokenVerificationError(
            code="invalid_token",
            message="The provided Firebase ID token is invalid or has expired.",
        ) from exc

    uid = decoded.get("uid") if isinstance(decoded, dict) else getattr(decoded, "uid", None)
    if not uid:
        logger.error(
            "Firebase token decoded but uid is absent: decoded=%r", decoded
        )
        raise TokenVerificationError(
            code="internal_error",
            message="Token decoded but Firebase UID is absent; contact support.",
        )

    return uid
