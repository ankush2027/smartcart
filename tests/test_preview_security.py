"""Preview admission tests use disposable SQLite and a fake Firebase reference."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from edge.cloud.app import create_cloud_app
from edge.cloud.preview import PreviewLimiter, RateLimits, preview_settings
from edge.cloud.outbox import install, Publisher
from tests.test_demo_software_integration import isolated, Reference


@pytest.fixture(autouse=True)
def isolate_database_for_preview_tests(isolated):
    """Keep outbox installation in disposable SQLite for every test here."""


def make(users=('a', 'b', 'manager'), **kwargs):
    install()
    def verify(token):
        if token == 'invalid':
            raise ValueError('private verifier detail')
        return {'uid': token.split(':')[0], 'role': 'manager' if token == 'manager' else 'customer'}
    return create_cloud_app(verifier=kwargs.pop('verifier', verify),
                            publisher=Publisher(Reference()), preview_uids=users, **kwargs).test_client()


def test_verified_customer_demo_admits_new_uid_without_client_uid_trust():
    client = make(('a', 'b', 'manager'), allow_all_verified_customers=True)
    response = call(client, token='new-customer')
    assert response.status_code == 200
    sid = response.json['session_id']
    assert call(client, f'/sessions/{sid}/checkout', token='new-customer', method='get').status_code == 200
    assert call(client, f'/sessions/{sid}/checkout', token='a', method='get').status_code == 403
    assert call(client, token=None, headers={'X-User-ID': 'new-customer'}).status_code == 401
    assert call(client, token='invalid').status_code == 401
    assert call(client, '/manager/service-status', token='new-manager', method='get').status_code == 403


def test_new_verified_customer_registration_is_idempotent_and_uses_only_verified_uid():
    from edge.database.database import get_connection
    client = make(('a',), allow_all_verified_customers=True)
    first = call(client, '/customer/register', token='new-google-customer')
    assert first.status_code == 200 and first.json == {'status': 'registered'}
    second = call(client, '/customer/register', token='new-google-customer')
    assert second.status_code == 200
    connection = get_connection()
    try:
        rows = connection.execute('SELECT customer_uid, count(*) FROM customers GROUP BY customer_uid').fetchall()
        assert rows == [('new-google-customer', 1)]
    finally:
        connection.close()
    assert call(client, '/customer/register', token=None).status_code == 401
    assert call(client, '/customer/register', token='invalid').status_code == 401
    forged = call(client, '/customer/register', token='new-google-customer', json={'uid': 'victim'})
    assert forged.status_code == 400
    assert call(client, '/customer/register', token='manager').status_code == 403


def test_new_verified_customer_can_pair_enabled_second_cart_and_unknown_cart_is_clear_404():
    client = make(('a',), allow_all_verified_customers=True)
    paired = call(client, token='new-customer', json={'cart_id': 'CART-002'})
    assert paired.status_code == 200
    assert paired.json['cart_id'] == 'CART-002'
    assert paired.json['status'] == 'paired'

    unknown = call(client, token='another-customer', json={'cart_id': 'CART-GHOST'})
    assert unknown.status_code == 404
    assert unknown.json['error'] == 'cart_not_allowed'


def test_open_customer_mode_keeps_payment_allowlist_and_rate_limits():
    client = make(('a',), allow_all_verified_customers=True, demo=True,
                  demo_allowed_uids={'a'},
                  rate_limits=RateLimits(), rate_clock=lambda: 0)
    assert call(client, token='new-customer').status_code == 200
    assert call(client, '/sessions/1/checkouts/1/payment', token='new-customer', method='get').status_code == 403
    limited = make(('a',), allow_all_verified_customers=True,
                   rate_limits=RateLimits(user_burst=1), rate_clock=lambda: 0)
    assert call(limited, token='new-customer').status_code == 200
    assert call(limited, token='new-customer').status_code == 429


def call(client, path='/pair-cart', token='a', method='post', **kwargs):
    headers = kwargs.pop('headers', {})
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    if method == 'post' and 'json' not in kwargs:
        kwargs['json'] = {'cart_id': 'CART-001'} if path == '/pair-cart' else {}
    return getattr(client, method)(path, base_url='https://preview.test', headers=headers, **kwargs)


def test_default_deny_and_authentication(caplog):
    client = make(())
    for token, expected in [(None, 401), ('invalid', 401), ('a', 403), ('manager', 403)]:
        assert call(client, token=token).status_code == expected
    assert 'private verifier detail' not in caplog.text


def test_roles_and_customer_isolation():
    client = make()
    first = call(client)
    assert first.status_code == 200
    sid = first.json['session_id']
    conflict = call(client, token='b')
    assert conflict.status_code == 409
    assert conflict.json['error'] == 'cart_already_assigned'
    assert call(client, f'/sessions/{sid}/checkout', token='b', method='get').status_code == 403
    assert call(client, f'/sessions/{sid}/checkout', method='get').status_code == 200
    assert call(client, '/manager/service-status', method='get').status_code == 403
    assert call(client, '/manager/service-status', token='manager', method='get').status_code == 200
    assert call(client, token='manager').status_code == 403
    assert call(make(('a',)), '/manager/service-status', token='manager', method='get').status_code == 403


@pytest.mark.parametrize('path,method', [('/pair-cart','post'), ('/sessions/1/checkout','get'),
    ('/sessions/1/checkout','post'), ('/sessions/1/checkouts/x/cancel','post'),
    ('/sessions/1/checkouts/x/payment','get'), ('/sessions/1/checkouts/x/payment','post'),
    ('/sessions/1/checkouts/x/payment/evidence','post'),
    ('/demo/sessions/1/checkouts/x/simulate-payment-success','post')])
def test_every_shopping_family_requires_preview_membership(path, method):
    client = make(('a',), demo=True, demo_allowed_uids={'outsider'})
    response = call(client, path, token='outsider', method=method,
                    headers={'X-Forwarded-For':'127.0.0.1', 'CF-Connecting-IP':'127.0.0.1', 'X-User-ID':'a'})
    assert response.status_code == 403
    assert response.json['error'] == 'preview_access_denied'


def test_global_limit_precedes_auth_and_ignores_identity_headers():
    clock = [0.0]
    calls = []
    def invalid(token):
        calls.append(token)
        raise ValueError()
    client = make(verifier=invalid, rate_limits=RateLimits(global_per_minute=60, global_burst=2), rate_clock=lambda:clock[0])
    assert call(client, token='x').status_code == 401
    assert call(client, token='y').status_code == 401
    response = call(client, token='z', headers={'X-Forwarded-For':'different', 'CF-Connecting-IP':'other'})
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '1'
    assert response.headers['Cache-Control'] == 'no-store'
    assert calls == ['x', 'y']
    clock[0] = 1
    assert call(client, token='z').status_code == 401


def test_health_unknown_and_missing_tokens_share_global_bound():
    client = make(rate_limits=RateLimits(global_burst=3), rate_clock=lambda:0)
    assert client.get('/health/live').status_code == 200
    assert call(client, '/unknown', method='get').status_code == 404
    assert call(client, token=None).status_code == 401
    assert client.get('/health/live').status_code == 429


def test_uid_quota_survives_token_rotation_and_is_separate_per_user():
    clock = [0.0]
    client = make(rate_limits=RateLimits(user_burst=1), rate_clock=lambda:clock[0])
    assert call(client, token='a:first').status_code == 200
    response = call(client, token='a:second')
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '2'
    assert call(client, token='b').status_code == 409
    clock[0] = 2
    assert call(client, token='a:third').status_code == 200


def test_demo_disabled_and_separate_demo_approval_required():
    client = make()
    assert call(client, '/manager/service-status', token='manager', method='get').json['demo_payment'] is False
    assert call(client, '/demo/sessions/1/checkouts/x/simulate-payment-success').status_code == 404
    client = make(demo=True, demo_allowed_uids={'b'})
    response = call(client, '/sessions/1/checkouts/x/payment')
    assert response.status_code == 403
    assert response.json['error'] == 'demo_not_allowed'


def test_oversize_token_does_not_reach_verifier():
    def never(token):
        pytest.fail('Oversize token reached verifier')
    assert call(make(verifier=never), token='x'*8193).status_code == 401


def test_concurrent_admission_and_bounded_memory():
    limiter = PreviewLimiter({'a'}, RateLimits(global_burst=10), clock=lambda:0)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _:limiter.global_retry_after(), range(100)))
    assert results.count(0) == 10
    for i in range(100):
        assert limiter.user_retry_after(str(i)) == 0
    assert len(limiter.per_user) <= 101


@pytest.mark.parametrize('settings', [ {'SMARTCART_PREVIEW_UIDS':value} for value in
    ['null', '"uid"', '[""]', '[" padded"]', '[123]', 'not-json', '['+','.join('"x"' for _ in range(101))+']'] ] +
    [{'SMARTCART_RATE_GLOBAL_BURST':'0'}, {'SMARTCART_RATE_GLOBAL_PER_MINUTE':'6001'},
     {'SMARTCART_RATE_USER_BURST':'-1'}, {'SMARTCART_RATE_USER_PER_MINUTE':'no'}])
def test_invalid_configuration_fails_closed(settings):
    with pytest.raises(ValueError, match='Invalid preview'):
        preview_settings(settings)


def test_environment_defaults_and_explicit_configuration():
    users, limits = preview_settings({})
    assert users == frozenset()
    assert limits == RateLimits()
    users, limits = preview_settings({'SMARTCART_PREVIEW_UIDS':'["a","manager"]', 'SMARTCART_RATE_USER_BURST':'3'})
    assert users == {'a','manager'}
    assert limits.user_burst == 3


def test_local_launcher_preserves_security_and_passes_preview_settings(tmp_path, monkeypatch):
    from tests.test_local_backend import local_fixture
    from edge.cloud.local import local_environment
    from edge.cloud.run import validate_configuration, server_options
    root = local_fixture(tmp_path)
    monkeypatch.setenv('SMARTCART_PREVIEW_UIDS', '["a"]')
    monkeypatch.setenv('SMARTCART_RATE_GLOBAL_BURST', '7')
    config = local_environment(root)
    assert preview_settings(config)[0] == {'a'}
    assert preview_settings(config)[1].global_burst == 7
    assert config['SMARTCART_DEMO_PAYMENT'] == 'false'
    options = server_options(config)
    assert options['host'] == '127.0.0.1' and options['port'] == 5001
    assert options['trusted_proxy_headers'] == {'x-forwarded-proto'}
    validate_configuration(config)
    monkeypatch.setenv('SMARTCART_PREVIEW_UIDS', 'invalid-json')
    with pytest.raises(ValueError):
        validate_configuration(local_environment(root))
