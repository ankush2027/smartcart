"""Mocked HTTP only. Dummy secrets and real isolated SQLite service integration."""
import hashlib
import hmac
import json
import socket
from dataclasses import replace

import pytest

from edge.billing import checkout
from edge.payment.provider import InvalidEvidence, OrderRejected, OrderRequest, ProviderUnavailable
from edge.payment.razorpay import HttpResponse, RazorpayProvider
from edge.payment.service import PaymentService
from edge.payment.money import PaymentError
from tests.test_checkout import isolated, populated, sql, state


SECRET = 'dummy-api-secret-not-a-credential'
WEBHOOK = 'dummy-webhook-secret-not-a-credential'
SCOPE = 'razorpay:test:acc_dummy'
REQUEST = OrderRequest('a' * 32, 1, SCOPE, 2000, 'INR')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No network is permitted in Razorpay tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def response(body, status=200):
    return HttpResponse(status, json.dumps(body).encode())


def order(request=REQUEST, **changes):
    return dict({'entity': 'order', 'id': 'order_dummy', 'receipt': 'sc_' + request.payment_attempt_id,
                 'amount': request.amount_paise, 'currency': 'INR', 'partial_payment': False}, **changes)


def payment(**changes):
    return dict({'entity': 'payment', 'id': 'pay_dummy', 'order_id': 'order_dummy',
                 'amount': 2000, 'currency': 'INR', 'status': 'captured', 'captured': True,
                 'amount_refunded': 0, 'refund_status': None}, **changes)


def collection(items):
    return response({'entity': 'collection', 'count': len(items), 'items': items})


def signature(body, secret=SECRET):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class MockHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, 'Unexpected HTTP call'
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(method, url, kwargs)
        return result


def adapter(http=None, **kwargs):
    options = dict(mode='test', merchant_id='acc_dummy', key_id='rzp_test_dummy',
                   key_secret=SECRET, webhook_secret=WEBHOOK, transport=http or MockHttp(),
                   order_lookup=lambda scope, oid: REQUEST if (scope, oid) == (SCOPE, 'order_dummy') else None)
    options.update(kwargs)
    return RazorpayProvider(**options)


def test_creation_payload_and_transport_security():
    http = MockHttp(response(order()))
    result = adapter(http).create_order(REQUEST)
    assert result.order_id == 'order_dummy' and result.checkout_id == REQUEST.checkout_id
    method, url, args = http.calls[0]
    assert method == 'POST' and url == 'https://api.razorpay.com/v1/orders'
    assert args['json'] == {'amount': 2000, 'currency': 'INR', 'receipt': 'sc_' + 'a' * 32,
                            'partial_payment': False,
                            'notes': {'payment_attempt_id': 'a' * 32, 'checkout_id': '1'}}
    assert args['timeout'] == (3.05, 10.0)
    assert args['verify'] is True and args['allow_redirects'] is False and args['retries'] == 0
    assert args['headers']['Authorization'].startswith('Basic ')


def test_stable_receipt_on_reconstructed_adapter():
    first, second = MockHttp(response(order())), MockHttp(collection([]))
    adapter(first).create_order(REQUEST)
    assert adapter(second).recover_order(REQUEST) is None
    receipt = first.calls[0][2]['json']['receipt']
    assert len(receipt) <= 40 and second.calls[0][2]['params']['receipt'] == receipt


@pytest.mark.parametrize('amount', [1, 99])
def test_minimum_rejected_without_post(amount):
    http = MockHttp()
    with pytest.raises(OrderRejected):
        adapter(http).create_order(replace(REQUEST, amount_paise=amount))
    assert http.calls == []


def test_minimum_accepted():
    request = replace(REQUEST, amount_paise=100)
    assert adapter(MockHttp(response(order(request)))).create_order(request).amount_paise == 100


@pytest.mark.parametrize('changes', [dict(amount_paise=True), dict(amount_paise=20.0),
    dict(amount_paise=0), dict(currency='USD'), dict(provider_scope='razorpay:live:acc_dummy'),
    dict(checkout_id=True), dict(payment_attempt_id='../untrusted')])
def test_invalid_request_never_dispatches(changes):
    http = MockHttp()
    with pytest.raises(InvalidEvidence):
        adapter(http).create_order(replace(REQUEST, **changes))
    assert not http.calls


@pytest.mark.parametrize('result', [TimeoutError(SECRET), ConnectionError(SECRET),
    HttpResponse(200, b'broken'), HttpResponse(200, b'{"amount":1,"amount":2}'),
    HttpResponse(200, b'{"amount":NaN}'), response({}, 429), response({}, 500),
    response({}, 503), response({}, 401), response({}, 302), response({}, 400)])
def test_ambiguous_creation_no_retry_and_redaction(result):
    http = MockHttp(result)
    with pytest.raises(ProviderUnavailable) as error:
        adapter(http).create_order(REQUEST)
    assert len(http.calls) == 1
    assert SECRET not in str(error.value) and SECRET not in repr(error.value)


def test_authoritative_input_rejection():
    http = MockHttp(response({'error': {'code': 'BAD_REQUEST_ERROR',
        'source': 'business', 'step': 'payment_initiation',
        'reason': 'input_validation_failed', 'field': 'amount', 'description': SECRET}}, 400))
    with pytest.raises(OrderRejected) as error:
        adapter(http).create_order(REQUEST)
    assert SECRET not in str(error.value) and len(http.calls) == 1


def test_duplicate_receipt_error_is_not_terminal():
    http = MockHttp(response({'error': {'code': 'BAD_REQUEST_ERROR',
        'reason': 'input_validation_failed', 'field': 'receipt'}}, 400))
    with pytest.raises(ProviderUnavailable):
        adapter(http).create_order(REQUEST)


@pytest.mark.parametrize('changes', [dict(amount=2001), dict(amount=True), dict(amount=2000.0),
    dict(currency='USD'), dict(receipt='different'), dict(id='order_../x'), dict(entity='payment'),
    dict(partial_payment=True), dict(partial_payment=0), dict(account_id='acc_other'), dict(mode='live')])
def test_creation_mismatches_fail_closed(changes):
    with pytest.raises(InvalidEvidence):
        adapter(MockHttp(response(order(**changes)))).create_order(REQUEST)


def test_documented_response_can_omit_partial_payment():
    body = order()
    del body['partial_payment']
    assert adapter(MockHttp(response(body))).create_order(REQUEST).order_id == 'order_dummy'


def test_recovery_zero_never_creates():
    http = MockHttp(collection([]))
    assert adapter(http).recover_order(REQUEST) is None
    assert [c[0] for c in http.calls] == ['GET']


def test_recovery_one_refetches_exact_order():
    http = MockHttp(collection([order()]), response(order()))
    assert adapter(http).recover_order(REQUEST).order_id == 'order_dummy'
    assert [c[0] for c in http.calls] == ['GET', 'GET']
    assert http.calls[-1][1].endswith('/orders/order_dummy')


@pytest.mark.parametrize('items', [[order(), order(id='order_other')], [order(), order()],
                                  [order(amount=2001)], [order(currency='USD')]])
def test_recovery_conflicts_require_review(items):
    with pytest.raises(ProviderUnavailable, match='operator review'):
        adapter(MockHttp(collection(items))).recover_order(REQUEST)


def test_paginated_receipt_contains_search_requires_exact_match():
    unrelated = [order(receipt='prefix-sc_' + 'a' * 32, id='order_x' + str(i)) for i in range(100)]
    http = MockHttp(collection(unrelated), collection([order()]), response(order()))
    assert adapter(http).recover_order(REQUEST).order_id == 'order_dummy'
    assert [c[2]['params']['skip'] for c in http.calls[:2]] == [0, 100]


def test_second_page_conflict_after_first_match():
    page = [order()] + [order(receipt='unrelated', id='order_x' + str(i)) for i in range(99)]
    with pytest.raises(ProviderUnavailable, match='Multiple'):
        adapter(MockHttp(collection(page), collection([order(id='order_other')]))).recover_order(REQUEST)


@pytest.mark.parametrize('body', [{}, {'entity': 'collection', 'count': True, 'items': []},
    {'entity': 'collection', 'count': 1, 'items': []},
    {'entity': 'collection', 'count': 1, 'items': [None]}])
def test_invalid_lookup_response(body):
    with pytest.raises(ProviderUnavailable):
        adapter(MockHttp(response(body))).recover_order(REQUEST)


def test_recovery_page_limit_remains_unresolved():
    http = MockHttp(collection([order(receipt='unrelated')] * 100))
    with pytest.raises(ProviderUnavailable, match='incomplete'):
        adapter(http, max_recovery_pages=1).recover_order(REQUEST)


def test_recovery_refetch_identity_must_match():
    with pytest.raises(InvalidEvidence):
        adapter(MockHttp(collection([order()]), response(order(id='order_other')))).recover_order(REQUEST)


def test_valid_checkout_signature():
    evidence = {'payment_id': 'pay_dummy', 'signature': signature(b'order_dummy|pay_dummy')}
    assert adapter().verify_client_evidence('order_dummy', evidence) == 'pay_dummy'


@pytest.mark.parametrize('evidence', [None, {}, {'success': True},
    {'payment_id': 'pay_dummy', 'signature': '0' * 64},
    {'payment_id': 'pay_dummy', 'signature': '\u00e9' * 64},
    {'payment_id': 'pay_other', 'signature': signature(b'order_dummy|pay_dummy')},
    {'payment_id': 'pay_dummy', 'signature': signature(b'order_other|pay_dummy')},
    {'payment_id': 'pay_dummy', 'signature': signature(b'order_dummy|pay_dummy'), 'order_id': 'order_dummy'}])
def test_forged_altered_or_client_order_evidence(evidence):
    with pytest.raises(InvalidEvidence):
        adapter().verify_client_evidence('order_dummy', evidence)


def test_fetch_captured_normalizes_only_persisted_identity():
    http = MockHttp(response(payment(notes={'checkout_id': '999'})), response(order()))
    result = adapter(http).fetch_payment('pay_dummy')
    assert (result.payment_attempt_id, result.checkout_id, result.provider_scope,
            result.amount_paise, result.status) == ('a' * 32, 1, SCOPE, 2000, 'CAPTURED')


@pytest.mark.parametrize('changes', [dict(id='pay_other'), dict(order_id='order_other'),
    dict(amount=1999), dict(amount=2000.0), dict(amount=True), dict(currency='USD'),
    dict(status='authorized', captured=False), dict(status='failed', captured=False),
    dict(status='refunded'), dict(captured=False), dict(captured=1), dict(amount_refunded=1),
    dict(amount_refunded=False), dict(refund_status='partial'), dict(account_id='acc_other'), dict(mode='live')])
def test_payment_mismatch_never_normalized(changes):
    with pytest.raises(InvalidEvidence):
        adapter(MockHttp(response(payment(**changes)), response(order()))).fetch_payment('pay_dummy')


@pytest.mark.parametrize('field', ['amount', 'currency', 'captured', 'amount_refunded', 'refund_status', 'status'])
def test_missing_payment_fields_fail_closed(field):
    body = payment()
    del body[field]
    with pytest.raises(InvalidEvidence):
        adapter(MockHttp(response(body), response(order()))).fetch_payment('pay_dummy')


def test_equal_amount_order_substitution_fails_receipt_binding():
    with pytest.raises(InvalidEvidence):
        adapter(MockHttp(response(payment()), response(order(receipt='sc_' + 'b' * 32)))).fetch_payment('pay_dummy')


def webhook_body(**changes):
    return json.dumps(dict({'entity': 'event', 'account_id': 'acc_dummy', 'event': 'payment.captured',
                           'payload': {'payment': {'entity': payment()}}}, **changes)).encode()


def test_webhook_authentication_only_no_http():
    http = MockHttp()
    body = webhook_body()
    assert adapter(http).verify_webhook(body, signature(body, WEBHOOK), event_id='evt_dummy') == ('evt_dummy', 'pay_dummy')
    assert not http.calls


@pytest.mark.parametrize('kind', ['changed_body', 'api_secret', 'signature', 'no_header', 'wrong_account',
                                  'wrong_mode', 'malformed', 'unsupported', 'wrong_entity'])
def test_invalid_webhook(kind):
    body, event_id = webhook_body(), 'evt_dummy'
    if kind == 'wrong_account': body = webhook_body(account_id='acc_other')
    if kind == 'wrong_mode': body = webhook_body(mode='live')
    if kind == 'malformed': body = b'not json'
    if kind == 'unsupported': body = webhook_body(event='order.paid')
    if kind == 'wrong_entity': body = webhook_body(payload={'payment': {'entity': {}}})
    signed = signature(body, SECRET if kind == 'api_secret' else WEBHOOK)
    if kind == 'changed_body': body += b' '
    if kind == 'signature': signed = '0' * 64
    if kind == 'no_header': event_id = None
    with pytest.raises(InvalidEvidence):
        adapter().verify_webhook(body, signed, event_id=event_id)


@pytest.mark.parametrize('options', [dict(mode='production'), dict(mode='live'), dict(merchant_id='wrong'),
    dict(key_secret=''), dict(webhook_secret=''), dict(timeout=(0, 1)), dict(timeout=(float('inf'), 1)),
    dict(max_recovery_pages=0), dict(order_lookup=None), dict(transport=None)])
def test_configuration_fail_closed_and_redacted(options):
    with pytest.raises(ValueError) as error:
        adapter(**options)
    assert SECRET not in str(error.value)


def test_live_scope_cannot_accept_test_request():
    provider = adapter(mode='live', key_id='rzp_live_dummy')
    assert provider.scope == 'razorpay:live:acc_dummy'
    with pytest.raises(InvalidEvidence):
        provider.create_order(REQUEST)
    assert SECRET not in repr(provider) and SECRET not in repr(HttpResponse(400, SECRET.encode()))


def persisted_lookup(scope, oid):
    rows = sql('SELECT payment_attempt_id,checkout_id,provider_scope,amount_paise,currency '
               'FROM payment_attempts WHERE provider_scope=? AND provider_order_id=?', (scope, oid))
    return OrderRequest(*rows[0]) if len(rows) == 1 else None


def test_lost_response_restart_recovery_and_atomic_completion(populated):
    sid, _ = populated
    cid = checkout.begin_checkout(sid, 'owner', 'checkout').checkout_id
    remote = {}
    def lost_response(method, url, args):
        payload = args['json']
        remote.update(entity='order', id='order_dummy', **payload)
        # Independent writer proves the provider call holds no SQLite lock.
        sql('BEGIN EXCLUSIVE')
        raise TimeoutError(SECRET)
    first_http = MockHttp(lost_response)
    first = PaymentService(adapter(first_http, order_lookup=persisted_lookup))
    pending = first.start(sid, cid, 'owner', 'key')
    assert pending['state'] == 'CREATING' and pending['error_code'] == 'recovery_required'
    with pytest.raises(checkout.CheckoutError):
        checkout.cancel_checkout(sid, cid, 'owner')
    # Reconstruct both service and adapter; no adapter cache carries identity.
    http = MockHttp(collection([]), collection([remote]), response(remote),
                    response(payment()), response(remote))
    service = PaymentService(adapter(http, order_lookup=persisted_lookup))
    assert service.start(sid, cid, 'owner', 'key')['state'] == 'CREATING'
    ready = service.start(sid, cid, 'owner', 'key')
    assert ready['state'] == 'ORDER_READY' and ready['payment_attempt_id'] == pending['payment_attempt_id']
    result = service.submit_evidence(sid, cid, 'owner',
        {'payment_id': 'pay_dummy', 'signature': signature(b'order_dummy|pay_dummy')})
    assert result['projection_sync'] == 'not_attempted'
    assert sql('SELECT state FROM payment_attempts') == [('COMPLETED',)]
    assert sql('SELECT end_time FROM sessions WHERE session_id=?', (sid,))[0][0] is not None
    assert all(call[0] == 'GET' for call in http.calls) and len(first_http.calls) == 1


def test_authorized_payment_leaves_sqlite_unverified(populated):
    sid, _ = populated
    cid = checkout.begin_checkout(sid, 'owner', 'checkout').checkout_id
    remote = {}
    def create(method, url, args):
        remote.update(entity='order', id='order_dummy', **args['json'])
        return response(remote)
    http = MockHttp(create, response(payment(status='authorized', captured=False)), lambda *a: response(remote))
    service = PaymentService(adapter(http, order_lookup=persisted_lookup))
    assert service.start(sid, cid, 'owner', 'key')['state'] == 'ORDER_READY'
    before = state()
    with pytest.raises(PaymentError):
        service.submit_evidence(sid, cid, 'owner',
            {'payment_id': 'pay_dummy', 'signature': signature(b'order_dummy|pay_dummy')})
    assert state() == before


@pytest.mark.parametrize('result', [TimeoutError('dummy'), ConnectionError('dummy'),
    HttpResponse(200, b'broken'), response(order(amount=123)), response({}, 429), response({}, 500)])
def test_ambiguous_service_creation_retains_same_attempt(populated, result):
    sid, _ = populated
    cid = checkout.begin_checkout(sid, 'owner', 'checkout').checkout_id
    http = MockHttp(result, collection([]))
    service = PaymentService(adapter(http, order_lookup=persisted_lookup))
    first = service.start(sid, cid, 'owner', 'key')
    second = service.start(sid, cid, 'owner', 'key')
    assert first['payment_attempt_id'] == second['payment_attempt_id']
    assert second['state'] == 'CREATING' and second['error_code'] == 'recovery_required'
    assert [call[0] for call in http.calls] == ['POST', 'GET']
    assert sql('SELECT COUNT(*) FROM payment_attempts') == [(1,)]


@pytest.mark.parametrize('status', ['authorized', 'failed'])
def test_authenticated_webhook_is_only_a_hint(status):
    body = webhook_body(event='payment.' + status,
                        payload={'payment': {'entity': payment(status=status, captured=False)}})
    before = state()
    assert adapter().verify_webhook(body, signature(body, WEBHOOK), event_id='evt_dummy') == ('evt_dummy', 'pay_dummy')
    assert state() == before


@pytest.mark.parametrize('body', [b'{}', b'[]', b'{"entity":"event","entity":"event"}', b'x' * 1_048_577],
                         ids=['missing-fields', 'array', 'duplicate-keys', 'oversized'])
def test_signed_malformed_webhook_body(body):
    with pytest.raises(InvalidEvidence):
        adapter().verify_webhook(body, signature(body, WEBHOOK), event_id='evt_dummy')


def test_payment_lookup_failure_is_redacted():
    def unavailable(*args):
        raise RuntimeError(SECRET)
    with pytest.raises(ProviderUnavailable) as error:
        adapter(MockHttp(response(payment())), order_lookup=unavailable).fetch_payment('pay_dummy')
    assert SECRET not in str(error.value)
