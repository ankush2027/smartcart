"""Explicit, bounded webhook processing. Provider facts precede SQLite authority."""
import hashlib
import time

from edge.billing.checkout import CheckoutError
from . import store, webhook_store, work


def classify(error):
    """Only bounded codes are persisted; exception strings may contain secrets."""
    if isinstance(error, CheckoutError):
        if error.code in ('payment_verification_unavailable', 'payment_not_ready'):
            return 'RETRYABLE', 'provider_unavailable'
        if error.code in ('invalid_payment_evidence', 'payment_not_successful'):
            # Authorization may become capture later. Bounded retries also cover
            # provider-side eventual visibility; no invalid evidence is saved.
            return 'RETRYABLE', 'payment_not_captured'
        return 'QUARANTINED', 'payment_conflict'
    return 'RETRYABLE', 'processing_error'


class WebhookProcessor:
    def __init__(self, services, *, policy=None, clock=time.time):
        self.services = dict(services)
        if any(scope != service.provider.scope for scope, service in self.services.items()):
            raise ValueError('Service scope mismatch')
        self.policy, self.clock = policy or work.RetryPolicy(), clock

    def run_once(self, webhook_id, *, reclaim_expired=False, guard=None):
        row = work.claim('payment_webhooks', webhook_id, policy=self.policy, clock=self.clock,
                         reclaim_expired=reclaim_expired)
        if row is None:
            return 'SKIPPED'
        own_guard = work.guard('payment_webhooks', row, clock=self.clock)
        def fence(conn):
            own_guard(conn)
            if guard is not None:
                guard(conn)
        state, error = 'PROCESSED', None
        try:
            service = self.services.get(row['provider_scope'])
            if service is None:
                state, error = 'RETRYABLE', 'scope_unavailable'
            elif hashlib.sha256(row['raw_payload']).hexdigest() != row['payload_digest']:
                state, error = 'QUARANTINED', 'invalid_evidence'
            else:
                attempt = store.find_order(row['provider_scope'], row['provider_order_id'])
                if attempt is None:
                    state, error = 'RETRYABLE', 'unknown_order'
                elif attempt['state'] == 'COMPLETED':
                    # A delayed failure cannot downgrade an already consumed
                    # payment. A different payment ID still requires review.
                    if attempt['provider_payment_id'] != row['provider_payment_id']:
                        state, error = 'QUARANTINED', 'payment_conflict'
                else:
                    if attempt['state'] != 'VERIFIED':
                        service.verify_attempt(attempt['payment_attempt_id'], row['provider_payment_id'],
                                               order_id=row['provider_order_id'], guard=fence)
                    elif attempt['provider_payment_id'] != row['provider_payment_id']:
                        raise CheckoutError('payment_conflict', 'Conflicting payment identity')
                    # VERIFIED already records an authoritative fetch. Restart
                    # may finish it without depending on a provider outage.
                    service.complete(attempt['checkout_id'], attempt['payment_attempt_id'], guard=fence)
        except work.ClaimLost:
            return 'CLAIM_LOST'
        except Exception as exc:
            state, error = classify(exc)
        try:
            return work.finish('payment_webhooks', row, state, error=error, policy=self.policy, clock=self.clock)
        except work.ClaimLost:
            return 'CLAIM_LOST'

    def run_batch(self, *, limit=20, after_id=0, reclaim_expired=False):
        return [(identity, self.run_once(identity, reclaim_expired=reclaim_expired))
                for identity in webhook_store.pending(limit=limit, after_id=after_id)]
