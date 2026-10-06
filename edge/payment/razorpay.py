"""Explicitly injected Razorpay adapter; no default transport or activation.

The transport is trusted backend infrastructure. It must honor TLS, deadlines,
redirect and retry settings. order_lookup(scope, order_id) must read persisted
SQLite identity and close its transaction before returning an OrderRequest.
"""
import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .provider import (InvalidEvidence, OrderRejected, OrderRequest, ProviderOrder,
                       ProviderPayment, ProviderUnavailable)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes = field(repr=False)


class HttpTransport(Protocol):
    def request(self, method, url, *, headers, json, params, timeout,
                verify, allow_redirects, retries) -> HttpResponse: ...


def _matches(pattern, value):
    return isinstance(value, str) and len(value) <= 256 and re.fullmatch(pattern, value) is not None


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**53 - 1


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _decode(body):
    if type(body) is not bytes or not 0 < len(body) <= 1_048_576:
        raise ValueError()
    def invalid_constant(value):
        raise ValueError()
    value = json.loads(body, object_pairs_hook=_object, parse_constant=invalid_constant)
    if type(value) is not dict:
        raise ValueError()
    return value


class RazorpayProvider:
    def __init__(self, *, mode, merchant_id, key_id, key_secret, webhook_secret,
                 transport: HttpTransport,
                 order_lookup: Callable[[str, str], OrderRequest | None],
                 timeout=(3.05, 10.0), max_recovery_pages=100):
        if (mode not in ('test', 'live')
                or not _matches(r'acc_[A-Za-z0-9]{1,64}', merchant_id)
                or not _matches(r'rzp_' + mode + r'_[A-Za-z0-9]+', key_id)
                or not isinstance(key_secret, str) or not key_secret
                or not isinstance(webhook_secret, str) or not webhook_secret
                or not callable(order_lookup) or not callable(getattr(transport, 'request', None))
                or not isinstance(timeout, tuple) or len(timeout) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in timeout)
                or type(max_recovery_pages) is not int or not 1 <= max_recovery_pages <= 1000):
            raise ValueError('Invalid Razorpay configuration')
        self.scope = f'razorpay:{mode}:{merchant_id}'
        self._mode, self._merchant = mode, merchant_id
        self._secret, self._webhook_secret = key_secret.encode(), webhook_secret.encode()
        self._authorization = 'Basic ' + base64.b64encode(f'{key_id}:{key_secret}'.encode()).decode()
        self._transport, self._lookup = transport, order_lookup
        self._timeout, self._pages = timeout, max_recovery_pages

    def _request_identity(self, request):
        if (not isinstance(request, OrderRequest)
                or not _matches(r'[0-9a-f]{32}', request.payment_attempt_id)
                or not _integer(request.checkout_id, 1)
                or request.provider_scope != self.scope
                or not _integer(request.amount_paise, 1) or request.currency != 'INR'):
            raise InvalidEvidence('Invalid persisted payment identity')
        return 'sc_' + request.payment_attempt_id

    def _http(self, method, path, *, payload=None, params=None):
        try:
            response = self._transport.request(
                method, 'https://api.razorpay.com/v1/' + path,
                headers={'Authorization': self._authorization, 'Content-Type': 'application/json'},
                json=payload, params=params, timeout=self._timeout,
                verify=True, allow_redirects=False, retries=0)
            if not isinstance(response, HttpResponse) or type(response.status_code) is not int:
                raise ValueError()
            body = _decode(response.body)
        except Exception:
            raise ProviderUnavailable('Razorpay response unavailable') from None
        if response.status_code != 200:
            # Receipt conflicts, generic 4xx, throttling and server failures do
            # not establish absence of an order. Only explicit input rejection
            # for money fields is classified as terminal.
            error = body.get('error')
            if (method == 'POST' and response.status_code == 400 and type(error) is dict
                    and error.get('code') == 'BAD_REQUEST_ERROR'
                    and error.get('source') == 'business'
                    and error.get('step') == 'payment_initiation'
                    and error.get('reason') == 'input_validation_failed'
                    and error.get('field') in ('amount', 'currency')):
                raise OrderRejected('Razorpay rejected order input')
            raise ProviderUnavailable('Razorpay outcome unresolved')
        return body

    def _scope(self, entity):
        if (('account_id' in entity and entity['account_id'] != self._merchant)
                or ('mode' in entity and entity['mode'] != self._mode)):
            raise InvalidEvidence('Provider scope mismatch')

    def _order(self, entity, request, expected_id=None):
        receipt = self._request_identity(request)
        if type(entity) is not dict:
            raise InvalidEvidence('Invalid provider order')
        self._scope(entity)
        if (entity.get('entity') != 'order'
                or not _matches(r'order_[A-Za-z0-9]+', entity.get('id'))
                or (expected_id is not None and entity['id'] != expected_id)
                or entity.get('receipt') != receipt
                or not _integer(entity.get('amount'), 100)
                or entity['amount'] != request.amount_paise or entity.get('currency') != 'INR'
                or entity.get('partial_payment', False) is not False):
            raise InvalidEvidence('Provider order mismatch')
        return ProviderOrder(request.payment_attempt_id, request.checkout_id,
                             self.scope, request.amount_paise, 'INR', entity['id'])

    def create_order(self, request):
        receipt = self._request_identity(request)
        if request.amount_paise < 100:
            raise OrderRejected('Razorpay minimum is 100 paise')
        body = self._http('POST', 'orders', payload={
            'amount': request.amount_paise, 'currency': 'INR', 'receipt': receipt,
            'partial_payment': False,
            'notes': {'payment_attempt_id': request.payment_attempt_id,
                      'checkout_id': str(request.checkout_id)}})
        return self._order(body, request)

    def recover_order(self, request):
        receipt = self._request_identity(request)
        found = None
        for page in range(self._pages):
            body = self._http('GET', 'orders', params={'receipt': receipt, 'count': 100, 'skip': page * 100})
            items = body.get('items')
            if (body.get('entity') != 'collection' or type(items) is not list
                    or not _integer(body.get('count')) or body['count'] != len(items)
                    or len(items) > 100):
                raise ProviderUnavailable('Invalid order lookup response')
            for item in items:
                if type(item) is not dict or not isinstance(item.get('receipt'), str):
                    raise ProviderUnavailable('Invalid order lookup response')
                # The documented receipt filter is a contains search.
                if item['receipt'] != receipt:
                    continue
                try:
                    order = self._order(item, request)
                except InvalidEvidence:
                    raise ProviderUnavailable('Conflicting order; operator review required') from None
                if found is not None:
                    raise ProviderUnavailable('Multiple orders; operator review required')
                found = order
            if len(items) < 100:
                if found is None:
                    return None
                return self._order(self._http('GET', 'orders/' + found.order_id), request, found.order_id)
        raise ProviderUnavailable('Order lookup incomplete; operator review required')

    def verify_client_evidence(self, order_id, evidence):
        if (not _matches(r'order_[A-Za-z0-9]+', order_id) or type(evidence) is not dict
                or set(evidence) != {'payment_id', 'signature'}
                or not _matches(r'pay_[A-Za-z0-9]+', evidence.get('payment_id'))):
            raise InvalidEvidence('Invalid checkout evidence')
        self._signature(self._secret, (order_id + '|' + evidence['payment_id']).encode(), evidence['signature'])
        return evidence['payment_id']

    @staticmethod
    def _signature(secret, body, signature):
        if (not _matches(r'[0-9a-f]{64}', signature)
                or not hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).hexdigest(), signature)):
            raise InvalidEvidence('Invalid signature')

    def fetch_payment(self, payment_id):
        if not _matches(r'pay_[A-Za-z0-9]+', payment_id):
            raise InvalidEvidence('Invalid payment ID')
        body = self._http('GET', 'payments/' + payment_id)
        self._scope(body)
        order_id = body.get('order_id')
        if (body.get('entity') != 'payment' or body.get('id') != payment_id
                or not _matches(r'order_[A-Za-z0-9]+', order_id)):
            raise InvalidEvidence('Invalid provider payment')
        try:
            request = self._lookup(self.scope, order_id)
        except Exception:
            raise ProviderUnavailable('Persisted order lookup unavailable') from None
        self._request_identity(request)
        self._order(self._http('GET', 'orders/' + order_id), request, order_id)
        if (not _integer(body.get('amount'), 100) or body['amount'] != request.amount_paise
                or body.get('currency') != 'INR' or body.get('status') != 'captured'
                or body.get('captured') is not True
                or type(body.get('amount_refunded')) is not int or body['amount_refunded'] != 0
                or 'refund_status' not in body or body['refund_status'] is not None):
            raise InvalidEvidence('Payment is not an exact unrefunded capture')
        return ProviderPayment(request.payment_attempt_id, request.checkout_id, self.scope,
                               request.amount_paise, 'INR', order_id, payment_id, 'CAPTURED')

    def verify_webhook(self, body, signature, *, event_id=None):
        """Return (delivery ID, authenticated payment ID), never payment proof.

        Razorpay supplies event_id in x-razorpay-event-id, not the signed body.
        That header is an untrusted delivery hint; a future inbox must also bind
        the body digest and enforce payment identity. No ingestion occurs here.
        """
        if type(body) is not bytes or not 0 < len(body) <= 1_048_576:
            raise InvalidEvidence('Invalid webhook body')
        self._signature(self._webhook_secret, body, signature)
        try:
            event = _decode(body)
            payment = event['payload']['payment']['entity']
            if (event.get('entity') != 'event' or event.get('account_id') != self._merchant
                    or event.get('event') not in ('payment.captured', 'payment.authorized', 'payment.failed')
                    or type(payment) is not dict or payment.get('entity') != 'payment'
                    or event['event'] != 'payment.' + payment.get('status', '')
                    or not _matches(r'pay_[A-Za-z0-9]+', payment.get('id'))
                    or not _matches(r'order_[A-Za-z0-9]+', payment.get('order_id'))
                    or not _matches(r'[A-Za-z0-9_-]{1,256}', event_id)):
                raise ValueError()
            self._scope(event)
            self._scope(payment)
        except Exception:
            raise InvalidEvidence('Invalid webhook event') from None
        return event_id, payment['id']
