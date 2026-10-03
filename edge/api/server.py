"""
edge/api/server.py — SmartCart HTTP API server.

This module provides the Flask application and route definitions for the
SmartCart backend API.  It is the only HTTP-aware layer in the pairing flow.

Endpoints
---------
POST /pair-cart
    Pair an authenticated Firebase customer with a physical cart/session.
    See: docs/pairing_endpoint.md
POST /sessions/{session_id}/checkout
    Freeze the verified owner's authoritative bill. See: docs/checkout.md

Architecture
------------
- Authentication: Bearer token extracted here, verified by
  ``edge.pairing.token_verifier.verify_id_token``.
- Business logic: delegated entirely to ``edge.pairing.pair_cart``.
- All responses use JSON with a stable machine-readable error contract.
- No credentials, stack traces, or internal details are returned to clients.

Usage (production)
------------------
    from edge.firebase.client import initialize as firebase_initialize
    from edge.api.server import create_app

    firebase_initialize()  # must be called before handling requests
    app = create_app()
    # Prefer: python -m edge.api.run_server (Waitress production server)

Usage (testing)
---------------
    app = create_app(
        token_verifier=fake_verify_fn,
        firebase_sync_fn=fake_sync_fn,
    )
    client = app.test_client()
    response = client.post("/pair-cart", json={"cart_id": "CART-001"},
                           headers={"Authorization": "Bearer fake-token"})
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict

from flask import Flask, jsonify, request

from edge.pairing.pair_cart import PairingError, pair_customer_to_cart
from edge.pairing.token_verifier import TokenVerificationError, verify_id_token
from edge.billing.checkout import begin_checkout, CheckoutError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(
    *,
    token_verifier=None,
    firebase_sync_fn=None,
) -> Flask:
    """
    Create and configure the SmartCart Flask application.

    Parameters
    ----------
    token_verifier:
        Optional callable ``(token: str) -> str`` for Firebase ID-token
        verification.  When ``None``, the production Firebase Admin SDK path
        is used.  **For testing only.**
    firebase_sync_fn:
        Optional callable ``(session_id, customer_uid, cart_id) -> None`` for
        Firebase synchronization after a successful pairing.  When ``None``,
        the default sync using the live Firebase client is used.
        **For testing only.**

    Returns
    -------
    Flask
        Configured application instance.
    """
    app = Flask(__name__)
    app.config["TESTING"] = False
    app.config["DEBUG"] = False

    # Capture injection parameters in closures.
    _token_verifier = token_verifier
    _firebase_sync_fn = firebase_sync_fn

    # ------------------------------------------------------------------
    # POST /pair-cart
    # ------------------------------------------------------------------

    @app.route("/pair-cart", methods=["POST"])
    def pair_cart():
        """
        Pair an authenticated Firebase customer with a physical cart.

        Authentication
        --------------
        Bearer token in the Authorization header:
            Authorization: Bearer <Firebase-ID-token>

        Request body (JSON)
        -------------------
        {
            "cart_id": "<physical-cart-identifier>"
        }

        Success response (HTTP 200)
        ---------------------------
        {
            "session_id": 42,
            "cart_id": "CART-001",
            "status": "paired" | "already_paired"
        }

        Error responses
        ---------------
        See docs/pairing_endpoint.md for the full error contract.
        """
        # ----------------------------------------------------------
        # 1. Extract and verify Bearer token
        # ----------------------------------------------------------
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _error_response(
                "authentication_required",
                "Authorization: Bearer <Firebase-ID-token> header is required.",
                401,
            )

        token = auth_header[len("Bearer "):]

        try:
            customer_uid = _do_verify_token(token, _token_verifier)
        except TokenVerificationError as exc:
            status = 401 if exc.code == "authentication_required" else 403
            return _error_response(exc.code, exc.message, status)

        # ----------------------------------------------------------
        # 2. Parse request body
        # ----------------------------------------------------------
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error_response(
                "invalid_request",
                "Request body must be valid JSON.",
                400,
            )

        cart_id = body.get("cart_id")
        if not isinstance(cart_id, str) or not cart_id.strip():
            return _error_response(
                "invalid_cart_id",
                "cart_id must be a non-empty string.",
                400,
            )

        # ----------------------------------------------------------
        # 3. Pairing logic
        # ----------------------------------------------------------
        try:
            result = pair_customer_to_cart(
                customer_uid=customer_uid,
                cart_id=str(cart_id).strip(),
                _firebase_sync_fn=_firebase_sync_fn,
            )
        except PairingError as exc:
            return _error_response(exc.code, exc.message, exc.http_status)
        except Exception as exc:
            logger.exception(
                "pair_cart: unhandled exception for customer_uid=%r cart_id=%r: %s",
                customer_uid, cart_id, exc,
            )
            return _error_response(
                "internal_error",
                "An internal error occurred. Please retry.",
                500,
            )

        # ----------------------------------------------------------
        # 4. Return success response
        # ----------------------------------------------------------
        return jsonify({
            "session_id": result.session_id,
            "cart_id":    result.cart_id,
            "status":     result.status,
        }), 200

    @app.route('/sessions/<session_id>/checkout', methods=['POST'])
    def checkout(session_id):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return _error_response('authentication_required', 'Bearer token required.', 401)
        try:
            uid = _do_verify_token(auth_header[len('Bearer '):], _token_verifier)
        except TokenVerificationError as exc:
            return _error_response(exc.code, exc.message,
                                   401 if exc.code == 'authentication_required' else 403)
        if request.get_json(silent=True) != {}:
            return _error_response('invalid_request', 'Body must be an empty JSON object.', 400)
        if not session_id.isascii() or not session_id.isdecimal() or len(session_id) > 19:
            return _error_response('invalid_session_id', 'Invalid session ID.', 400)
        try:
            result = begin_checkout(int(session_id), uid)
        except CheckoutError as exc:
            return _error_response(exc.code, exc.message, exc.http_status)
        except sqlite3.Error:
            return _error_response('database_unavailable', 'Checkout unavailable; retry safely.', 503)
        except Exception:
            return _error_response('internal_error', 'Checkout could not be confirmed.', 500)
        return jsonify(asdict(result)), 200

    return app


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _do_verify_token(token: str, verifier) -> str:
    """Verify *token* using *verifier* (or default) and return the UID."""
    if verifier is not None:
        # Testing path: verifier is a callable (token) -> str directly.
        return verify_id_token(token, _verifier=verifier)
    return verify_id_token(token)


def _error_response(code: str, message: str, status: int):
    """Build a JSON error response."""
    return jsonify({"error": code, "message": message}), status
