"""Stage 1 regression tests: no sockets, live Firebase, or existing databases."""
from pathlib import Path
import pytest
from waitress.proxy_headers import proxy_headers_middleware
from werkzeug.test import Client
from werkzeug.wrappers import Response
from edge.cloud.local import local_environment
from edge.cloud.run import server_options, validate_configuration
from edge.cloud.app import create_cloud_app, verify_claims
from edge.cloud.outbox import Publisher, install
from tests.test_demo_software_integration import isolated, Reference


@pytest.fixture(autouse=True)
def isolate_database_for_backend_tests(isolated):
    """Never let install() in this module touch the operator's selected DB."""


def local_fixture(tmp_path):
    root = tmp_path / 'hardware'
    (root/'data').mkdir(parents=True)
    (root/'data/smartcart_demo.db').write_bytes(b'preserve-existing-bytes')
    credential = root/'credential.json'
    credential.write_text('{}')
    (root/'.env').write_text(
        'SMARTCART_FIREBASE_URL=https://example.test\n'
        f'GOOGLE_APPLICATION_CREDENTIALS="{credential}"\n'
        'SMARTCART_ALLOWED_CARTS=CART-001,CART-002\n'
        'SMARTCART_DATABASE_PATH=/another/worktree/database.db\n'
        'SMARTCART_API_HOST=0.0.0.0\nSMARTCART_TRUSTED_PROXY=*\n'
        'SMARTCART_DEMO_PAYMENT=true\n')
    return root


def test_local_configuration_ignores_legacy_database_and_unsafe_options(tmp_path):
    root = local_fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    config = local_environment(root)
    assert Path(config['SMARTCART_DATABASE_PATH']) == root/'data/smartcart_demo.db'
    assert config['SMARTCART_API_HOST'] == '127.0.0.1'
    assert config['PORT'] == '5001'
    assert config['SMARTCART_TRUSTED_PROXY'] == '127.0.0.1'
    assert config['SMARTCART_DEMO_PAYMENT'] == 'false'
    assert config['SMARTCART_ALLOW_ALL_VERIFIED_CUSTOMERS'] == 'true'
    assert config['SMARTCART_ALLOWED_CARTS'] == 'CART-001,CART-002'
    assert validate_configuration(config) == root/'data/smartcart_demo.db'
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_local_missing_database_never_created(tmp_path):
    root = local_fixture(tmp_path)
    (root/'data/smartcart_demo.db').unlink()
    with pytest.raises(RuntimeError, match='refusing creation'):
        local_environment(root)
    assert not (root/'data/smartcart_demo.db').exists()


def test_database_symlink_outside_workspace_rejected(tmp_path):
    root = local_fixture(tmp_path)
    db = root/'data/smartcart_demo.db'
    outside = tmp_path/'outside.db'
    outside.write_bytes(b'unchanged')
    db.unlink()
    db.symlink_to(outside)
    with pytest.raises(RuntimeError): local_environment(root)
    assert outside.read_bytes() == b'unchanged'


def test_config_is_data_not_shell_code(tmp_path):
    root = local_fixture(tmp_path)
    marker = tmp_path/'must-not-exist'
    with (root/'.env').open('a') as stream:
        stream.write(f'IGNORED=$(touch {marker})\n')
    local_environment(root)
    assert not marker.exists()


@pytest.mark.parametrize('proxy', ['*', '127.0.0.0/8', 'localhost', '127.0.0.1,10.0.0.1'])
def test_no_wildcard_or_ambiguous_proxy(proxy):
    with pytest.raises(RuntimeError, match='one explicit IP'):
        server_options({'SMARTCART_TRUSTED_PROXY': proxy})


@pytest.mark.parametrize('port', ['0','65536','abc'])
def test_invalid_listener_rejected(port):
    with pytest.raises(RuntimeError): server_options({'PORT':port})


def test_safe_listener_defaults_and_explicit_override():
    config = server_options({})
    assert config['host'] == '127.0.0.1' and config['port'] == 5001
    assert config['clear_untrusted_proxy_headers'] is True
    assert 'trusted_proxy' not in config
    assert server_options({'SMARTCART_API_HOST':'0.0.0.0','PORT':'10000'})['host'] == '0.0.0.0'


def proxy_client():
    app = create_cloud_app(verifier=lambda token: {'uid':'customer-a'}, publisher=Publisher(Reference()))
    options=server_options({'SMARTCART_TRUSTED_PROXY':'127.0.0.1'})
    middleware=proxy_headers_middleware(app, trusted_proxy=options['trusted_proxy'],
        trusted_proxy_count=options['trusted_proxy_count'],
        trusted_proxy_headers=options['trusted_proxy_headers'], clear_untrusted=True)
    return Client(middleware, Response)


@pytest.mark.parametrize('peer,proto,status', [
    ('127.0.0.1','https',401), ('127.0.0.1','http',400),
    ('127.0.0.1','https,http',400), ('192.0.2.10','https',400)])
def test_waitress_proxy_boundary_still_requires_auth(peer,proto,status):
    response=proxy_client().post('/pair-cart', json={'cart_id':'CART-001'},
        environ_overrides={'REMOTE_ADDR':peer}, headers={
            'X-Forwarded-Proto':proto, 'X-Forwarded-For':'127.0.0.1',
            'Forwarded':'for=127.0.0.1;proto=https'})
    assert response.status_code == status


def test_untrusted_host_and_client_identity_headers_stripped():
    seen={}
    def app(environ,start_response):
        seen.update(environ)
        start_response('200 OK',[])
        return [b'ok']
    client=Client(proxy_headers_middleware(app,trusted_proxy='127.0.0.1',
        trusted_proxy_headers={'x-forwarded-proto'},clear_untrusted=True),Response)
    client.get('/',environ_overrides={'REMOTE_ADDR':'127.0.0.1'},headers={
        'X-Forwarded-Proto':'https','X-Forwarded-Host':'attacker.test',
        'X-Forwarded-For':'victim','Forwarded':'host=attacker.test;proto=http'})
    assert seen['wsgi.url_scheme']=='https'
    assert seen['REMOTE_ADDR']=='127.0.0.1'
    assert not any(k in seen for k in ['HTTP_X_FORWARDED_HOST','HTTP_X_FORWARDED_FOR','HTTP_FORWARDED'])


def test_admin_verifier_checks_revocation(monkeypatch):
    from firebase_admin import auth
    calls=[]
    def fake(token, **kwargs):
        calls.append(kwargs)
        return {'uid':'approved'}
    monkeypatch.setattr(auth,'verify_id_token',fake)
    assert verify_claims('test-token')['uid']=='approved'
    assert calls==[{'check_revoked':True}]


def test_demo_requires_explicit_allowlist_in_startup(tmp_path):
    root=local_fixture(tmp_path)
    config=local_environment(root)
    config['SMARTCART_DEMO_PAYMENT']='true'
    with pytest.raises(RuntimeError,match='allowlist'):validate_configuration(config)
    config=local_environment(root,demo_uids=['customer-a'])
    assert config['SMARTCART_DEMO_ALLOWED_UIDS']=='customer-a'
    validate_configuration(config)


@pytest.mark.parametrize('suffix,body', [
    ('/payment',{}),('/payment/evidence',{'payment_id':'fake','signature':'fake'}),
    ('/simulate-payment-success',{})])
def test_unapproved_customer_cannot_use_any_fake_payment_path(suffix,body):
    install()
    app=create_cloud_app(verifier=lambda t:{'uid':'unapproved'}, publisher=Publisher(Reference()),
                         demo=True,demo_allowed_uids={'customer-a'},preview_uids={'unapproved'})
    path='/sessions/1/checkouts/1'+suffix
    if suffix=='/simulate-payment-success':path='/demo'+path
    response=app.test_client().post(path,base_url='https://test.example',json=body,
                                   headers={'Authorization':'Bearer test-token'})
    assert response.status_code==403
    assert response.json['error']=='demo_not_allowed'


def test_default_cloud_has_no_simulated_completion_route():
    app=create_cloud_app(verifier=lambda t:{'uid':'customer-a'},publisher=Publisher(Reference()))
    assert not any(rule.rule.startswith('/demo/') for rule in app.url_map.iter_rules())


def test_startup_failure_releases_lock_without_database_changes(tmp_path, monkeypatch):
    import fcntl
    from edge.cloud import run
    from edge.firebase import client
    root=local_fixture(tmp_path)
    config=local_environment(root)
    for key,value in config.items():monkeypatch.setenv(key,value)
    db=Path(config['SMARTCART_DATABASE_PATH'])
    before=db.read_bytes()
    handlers={handler:list(handler.filters) for handler in run.logging.getLogger().handlers}
    old_level=run.logging.getLogger('smartcart.cloud').level
    monkeypatch.setattr(run.logging,'basicConfig',lambda **_:None)
    def fail():raise RuntimeError('SDK setup unavailable')
    monkeypatch.setattr(client,'initialize',fail)
    try:
        with pytest.raises(RuntimeError,match='SDK setup unavailable'):run.main()
    finally:
        for handler,filters in handlers.items():handler.filters=filters
        run.logging.getLogger('smartcart.cloud').setLevel(old_level)
    with (db.parent/'smartcart-cloud.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert db.read_bytes()==before
