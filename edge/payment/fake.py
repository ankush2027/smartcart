"""Explicitly injected OFFLINE development adapter. Never a default provider.

FakeLedger models the remote service, not SQLite or application-process state.
Retain it across service/adapter reconstruction to simulate backend restart.
Actual OS-process persistence of a real remote service is outside this fake.
"""
from dataclasses import field, dataclass
import hashlib
import hmac
import json
from threading import RLock

from .provider import (AuthenticatedWebhook, ProviderOrder, ProviderPayment, ProviderUnavailable,
                       OrderRejected, InvalidEvidence)


@dataclass
class FakeLedger:
    orders: dict = field(default_factory=dict)
    payments: dict = field(default_factory=dict)
    rejected: set = field(default_factory=set)
    lock: object = field(default_factory=RLock)


class FakeProvider:
    scope = 'fake:test:smartcart'
    _signing_key = b'offline-fake-only-not-a-production-secret'

    def __init__(self, ledger=None, *, creation='success', recover_missing=False):
        self.ledger = ledger if ledger is not None else FakeLedger()
        self.creation = creation
        self.recover_missing = recover_missing
        self.create_calls = 0

    def _order(self, request):
        key = request.payment_attempt_id
        if key in self.ledger.rejected:
            raise OrderRejected()
        order = self.ledger.orders.get(key)
        if order is None:
            order = ProviderOrder(request.payment_attempt_id, request.checkout_id,
                                  request.provider_scope, request.amount_paise,
                                  request.currency, 'fake-order-' + key)
            self.ledger.orders[key] = order
        elif (order.checkout_id, order.provider_scope, order.amount_paise, order.currency) != (
                request.checkout_id, request.provider_scope, request.amount_paise, request.currency):
            raise InvalidEvidence()
        return order

    def create_order(self, request):
        with self.ledger.lock:
            self.create_calls += 1
            if self.creation == 'reject':
                if request.payment_attempt_id in self.ledger.orders:
                    return self._order(request)
                self.ledger.rejected.add(request.payment_attempt_id)
                raise OrderRejected()
            if self.creation == 'timeout':
                raise ProviderUnavailable()
            order = self._order(request)
            if self.creation == 'lost_response':
                raise ProviderUnavailable()
            return order

    def recover_order(self, request):
        with self.ledger.lock:
            if request.payment_attempt_id in self.ledger.rejected:
                raise OrderRejected()
            if request.payment_attempt_id in self.ledger.orders or self.recover_missing:
                # Atomic, idempotent remote operation even if create is in flight.
                # This is an explicit fake contract, not an absence/lease heuristic.
                return self._order(request)
            return None

    def fetch_order(self, order_id):
        with self.ledger.lock:
            for order in self.ledger.orders.values():
                if order.order_id == order_id:
                    return order
        raise InvalidEvidence()

    def record_payment(self, order_id, *, status='CAPTURED', suffix='1'):
        """Test harness control; never exposed by HTTP or evidence processing."""
        with self.ledger.lock:
            order = self.fetch_order(order_id)
            payment = ProviderPayment(order.payment_attempt_id, order.checkout_id,
                                      order.provider_scope, order.amount_paise,
                                      order.currency, order_id, order_id + '-payment-' + suffix, status)
            self.ledger.payments[payment.payment_id] = payment
            return payment

    def fetch_payment(self, payment_id):
        with self.ledger.lock:
            payment = self.ledger.payments.get(payment_id)
            if payment is None:
                raise InvalidEvidence()
            return payment

    def _signature(self, value):
        return hmac.new(self._signing_key, value, hashlib.sha256).hexdigest()

    def evidence(self, payment_id):
        payment = self.fetch_payment(payment_id)
        value = (payment.order_id + '|' + payment_id).encode()
        return {'payment_id': payment_id, 'signature': self._signature(value)}

    def verify_client_evidence(self, order_id, evidence):
        if (type(evidence) is not dict or set(evidence) != {'payment_id', 'signature'}
                or any(type(v) is not str or not 1 <= len(v) <= 256 for v in evidence.values())
                or not evidence['signature'].isascii()):
            raise InvalidEvidence()
        value = (order_id + '|' + evidence['payment_id']).encode()
        if not hmac.compare_digest(self._signature(value), evidence['signature']):
            raise InvalidEvidence()
        return evidence['payment_id']

    def verify_webhook(self, body, signature):
        if (type(body) is not bytes or len(body) > 4096 or type(signature) is not str
                or not signature.isascii()
                or not hmac.compare_digest(self._signature(body), signature)):
            raise InvalidEvidence()
        try:
            event = json.loads(body)
            if set(event) not in ({'event_id', 'payment_id'},
                                 {'event_id', 'payment_id', 'order_id', 'event_type'}) or any(
                    type(v) is not str or not 1 <= len(v) <= 256 for v in event.values()):
                raise ValueError
            return event['event_id'], event['payment_id']
        except (ValueError, TypeError, AttributeError):
            raise InvalidEvidence() from None

    def authenticate_webhook(self, body, signature, *, event_id):
        verified_id, payment_id = self.verify_webhook(body, signature)
        event = json.loads(body)
        if (event_id != verified_id or set(event) != {'event_id', 'payment_id', 'order_id', 'event_type'}
                or event['event_type'] not in ('payment.captured', 'payment.authorized', 'payment.failed')):
            raise InvalidEvidence()
        return AuthenticatedWebhook(self.scope, event_id, event['event_type'], event['order_id'], payment_id)
