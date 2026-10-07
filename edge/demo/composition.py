"""
edge/demo/composition.py — Explicit software-demo payment composition.

SAFETY NOTICE
-------------
- FakeProvider is for development/demo only and does NOT process real payments.
- Real Razorpay network calls NEVER occur in this composition.
- Production/normal startup does NOT use this module and does NOT construct FakeProvider.
- All transactions pass through PaymentService, preserving checkout bindings, idempotency,
  evidence verification, and SQLite authority.
"""
from __future__ import annotations

import logging
from typing import Callable

from flask import Flask, jsonify, request

from edge.api.server import create_app, _do_verify_token, _error_response
from edge.billing.checkout import CheckoutError
from edge.billing.session_projection import read_session_projection
from edge.firebase.reconcile import reconcile_session_to_firebase
from edge.pairing.token_verifier import TokenVerificationError
from edge.payment.fake import FakeLedger, FakeProvider
from edge.payment.money import DEFAULT_MAX_PAISE, PaymentError
from edge.payment.service import PaymentService

logger = logging.getLogger(__name__)


def create_demo_payment_service(
    ledger: FakeLedger | None = None,
    *,
    firebase_ref=None,
    completion_sync: Callable[[int], None] | None = None,
    max_paise: int = DEFAULT_MAX_PAISE,
) -> tuple[PaymentService, FakeProvider]:
    """
    Explicit demo composition wiring PaymentStore, FakeProvider, and PaymentService.

    Parameters
    ----------
    ledger:
        Optional FakeLedger instance. If None, a fresh in-memory ledger is created.
    firebase_ref:
        Optional offline Firebase root reference for tests.
    completion_sync:
        Optional explicit completion projection hook. If None, uses a default hook
        that calls reconcile_session_to_firebase(session_id, _ref=firebase_ref).
    max_paise:
        Cap on payment amount in paise.

    Returns
    -------
    tuple[PaymentService, FakeProvider]
    """
    fake_ledger = ledger if ledger is not None else FakeLedger()
    fake_provider = FakeProvider(ledger=fake_ledger)

    def default_completion_sync(session_id: int) -> None:
        """Project authoritative completed session to Firebase."""
        logger.info("Demo completion hook: projecting session_id=%d to Firebase", session_id)
        try:
            reconcile_session_to_firebase(session_id, _ref=firebase_ref)
            logger.info("Demo completion hook: Firebase projection succeeded for session_id=%d", session_id)
        except Exception as exc:
            logger.warning(
                "Demo completion hook: Firebase projection failed for session_id=%d: %s. "
                "SQLite payment completion remains authoritative; reconciliation required.",
                session_id,
                exc,
            )
            raise

    sync_hook = completion_sync if completion_sync is not None else default_completion_sync

    service = PaymentService(
        provider=fake_provider,
        max_paise=max_paise,
        completion_sync=sync_hook,
    )
    logger.info("Demo payment composition initialized (scope: %s)", fake_provider.scope)
    return service, fake_provider


def simulate_demo_payment_success(
    payment_service: PaymentService,
    fake_provider: FakeProvider,
    session_id: int,
    checkout_id: int,
    uid: str,
) -> dict:
    """
    Controlled demo payment completion helper.

    IMPORTANT: Exercises the existing payment state machine and verification pipeline
    via FakeProvider evidence rather than directly editing SQLite into a paid state.
    """
    # 1. Read current payment attempt
    attempt = payment_service.status(session_id, checkout_id, uid)

    # 2. If still in CREATING state, recover it
    if attempt.get("state") == "CREATING":
        attempt = payment_service.recover_creation(attempt["payment_attempt_id"])

    order_id = attempt.get("provider_order_id")
    if not order_id:
        raise PaymentError(
            "order_not_found",
            f"No provider order found for session {session_id} checkout {checkout_id}",
        )

    # 3. Simulate payment capture on FakeProvider ledger
    provider_payment = fake_provider.record_payment(order_id, status="CAPTURED")

    # 4. Generate authentic fake client evidence
    evidence = fake_provider.evidence(provider_payment.payment_id)

    # 5. Submit evidence through normal PaymentService pipeline:
    #    verify -> fetch_payment -> save_verified -> complete -> completion_sync
    result = payment_service.submit_evidence(session_id, checkout_id, uid, evidence)
    logger.info(
        "Demo payment simulated successfully: session_id=%d checkout_id=%d payment_id=%s",
        session_id,
        checkout_id,
        provider_payment.payment_id,
    )
    return result


def create_demo_app(
    *,
    token_verifier=None,
    firebase_sync_fn=None,
    payment_service: PaymentService | None = None,
    fake_provider: FakeProvider | None = None,
    firebase_ref=None,
) -> Flask:
    """
    Factory for the explicit demo Flask app.

    Wires create_app with demo PaymentService, and exposes a controlled demo-only
    endpoint to trigger simulated payment success.
    """
    if payment_service is None or fake_provider is None:
        ps, fp = create_demo_payment_service(firebase_ref=firebase_ref)
        if payment_service is None:
            payment_service = ps
        if fake_provider is None:
            fake_provider = fp

    app = create_app(
        token_verifier=token_verifier,
        firebase_sync_fn=firebase_sync_fn,
        payment_service=payment_service,
    )

    _token_verifier = token_verifier
    _payment_service = payment_service
    _fake_provider = fake_provider

    @app.route(
        "/demo/sessions/<session_id>/checkouts/<checkout_id>/simulate-payment-success",
        methods=["POST"],
    )
    def demo_simulate_payment(session_id: str, checkout_id: str):
        """
        Explicit demo-only helper to simulate successful fake payment completion.
        Requires Bearer token matching session owner.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _error_response("authentication_required", "Bearer token required.", 401)

        try:
            uid = _do_verify_token(auth_header[len("Bearer "):], _token_verifier)
        except TokenVerificationError as exc:
            return _error_response(
                exc.code,
                exc.message,
                401 if exc.code == "authentication_required" else 403,
            )

        if not session_id.isdecimal() or not checkout_id.isdecimal():
            return _error_response("invalid_identifier", "Invalid session or checkout ID.", 400)

        try:
            result = simulate_demo_payment_success(
                _payment_service,
                _fake_provider,
                int(session_id),
                int(checkout_id),
                uid,
            )
            return jsonify(result), 200
        except CheckoutError as exc:
            return _error_response(exc.code, exc.message, exc.http_status)
        except PaymentError as exc:
            return _error_response(exc.code, exc.message, exc.http_status)
        except Exception as exc:
            logger.exception("Demo simulate payment failed: %s", exc)
            return _error_response("demo_payment_failed", "Demo payment simulation failed.", 500)

    return app
