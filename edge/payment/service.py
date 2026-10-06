"""Explicitly injected provider orchestration; all external work follows commit."""
from . import store
from .money import PaymentError, DEFAULT_MAX_PAISE, validate_cap
from .provider import OrderRequest, OrderRejected, InvalidEvidence


def public_payment(attempt):
    """Response allowlist: no request key, account scope, evidence or raw errors."""
    return {key: attempt[key] for key in (
        'payment_attempt_id', 'checkout_id', 'amount_paise', 'currency', 'state',
        'provider_order_id', 'created_at', 'verified_at', 'completed_at', 'error_code')}


class PaymentService:
    def __init__(self, provider, *, max_paise=DEFAULT_MAX_PAISE, completion_sync=None):
        """No default provider or live Firebase fallback.

        completion_sync(session_id) is an optional explicit post-commit hook.
        Controlled reconciliation repairs failed/omitted projection delivery.
        """
        validate_cap(max_paise)
        store._key(provider.scope)
        self.provider = provider
        self.max_paise = max_paise
        self.completion_sync = completion_sync

    def _request(self, attempt):
        if attempt['provider_scope'] != self.provider.scope:
            store.integrity()
        return OrderRequest(attempt['payment_attempt_id'], attempt['checkout_id'],
                            attempt['provider_scope'], attempt['amount_paise'], attempt['currency'])

    def start(self, session_id, checkout_id, uid, request_key):
        attempt = store.begin_payment(session_id, checkout_id, uid, request_key,
                                      provider_scope=self.provider.scope, max_paise=self.max_paise)
        attempt, claimed = store.claim_dispatch(attempt['payment_attempt_id'])
        if attempt['state'] != 'CREATING':
            return public_payment(attempt)
        request = self._request(attempt)
        try:
            order = (self.provider.create_order(request) if claimed
                     else self.provider.recover_order(request))
        except OrderRejected:
            return public_payment(store.creation_problem(attempt['payment_attempt_id'], rejected=True))
        except Exception:
            # Exceptions, including unexpected adapter failures, do not prove
            # that the provider failed to create an order. Never log raw data.
            return public_payment(store.creation_problem(attempt['payment_attempt_id']))
        if order is None:
            return public_payment(store.creation_problem(attempt['payment_attempt_id']))
        return public_payment(store.save_order(attempt['payment_attempt_id'], order))

    def status(self, session_id, checkout_id, uid):
        return public_payment(store.read_payment(session_id, checkout_id, uid))

    def verify(self, session_id, checkout_id, uid, evidence):
        """Authenticate an evidence hint, then independently retrieve payment facts."""
        attempt = store.read_payment(session_id, checkout_id, uid)
        self._request(attempt)
        if attempt['state'] not in ('ORDER_READY', 'VERIFIED', 'COMPLETED'):
            raise PaymentError('payment_not_ready', 'Recover the existing payment order first.')
        try:
            payment_id = self.provider.verify_client_evidence(attempt['provider_order_id'], evidence)
            payment = self.provider.fetch_payment(payment_id)
        except InvalidEvidence:
            raise PaymentError('invalid_payment_evidence', 'Payment evidence could not be authenticated.', 400) from None
        except Exception:
            raise PaymentError('payment_verification_unavailable', 'Payment verification is unavailable; retry safely.', 503) from None
        if payment.payment_id != payment_id:
            raise PaymentError('payment_evidence_mismatch', 'Provider returned a different payment.')
        return public_payment(store.save_verified(attempt['payment_attempt_id'], payment))

    def complete(self, checkout_id, attempt_id):
        result, fresh = store.complete_paid_checkout(checkout_id, attempt_id)
        result['projection_sync'] = 'not_attempted'
        if fresh and self.completion_sync is not None:
            try:
                self.completion_sync(result['session_id'])
                result['projection_sync'] = 'succeeded'
            except Exception:
                result['projection_sync'] = 'reconciliation_required'
        return result

    def submit_evidence(self, session_id, checkout_id, uid, evidence):
        verified = self.verify(session_id, checkout_id, uid, evidence)
        return self.complete(checkout_id, verified['payment_attempt_id'])
