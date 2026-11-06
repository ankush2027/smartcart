"""Offline cloud boundary/persistence tests. Never use live Firebase credentials."""
import pytest
from edge.cloud.app import create_cloud_app
from edge.cloud.demo_provider import PersistentDemoProvider
from edge.cloud.outbox import install, Publisher
from edge.database.database import get_connection
from edge.billing.billing import add_item
from edge.billing.checkout import begin_checkout, read_checkout_state
from edge.demo.composition import simulate_demo_payment_success
from edge.payment.service import PaymentService
from tests.test_demo_software_integration import isolated, Reference, pair, runtime


def app_for(root):
    install()
    def verify(token):
        if token == 'expired': raise ValueError('secret that must not be logged')
        return {'uid': token, 'role': 'manager' if token == 'manager' else 'customer'}
    return create_cloud_app(verifier=verify, publisher=Publisher(root), preview_uids={'customer-a', 'customer-b', 'manager'}, demo=True, demo_allowed_uids={'customer-a'})


def call(client, method, path, who='customer-a', **kwargs):
    return getattr(client, method)(path, base_url='https://cloud.test',
        headers={'Authorization': 'Bearer '+who, **kwargs.pop('headers', {})}, **kwargs)


def test_auth_roles_https_cors_and_health(caplog):
    client = app_for(Reference()).test_client()
    assert client.get('/health/live').status_code == 200
    assert client.get('/health/ready').status_code == 200
    assert client.post('/pair-cart', json={}).status_code == 400
    assert client.post('/pair-cart', base_url='https://cloud.test', json={}).status_code == 401
    assert call(client, 'post', '/pair-cart', 'expired', json={}).status_code == 401
    assert call(client, 'post', '/pair-cart', 'manager', json={'cart_id':'CART-001'}).status_code == 403
    assert call(client, 'get', '/manager/service-status').status_code == 403
    assert call(client, 'get', '/manager/service-status', 'manager').status_code == 200
    assert call(client, 'post', '/pair-cart', json={}, headers={'Origin':'https://evil.test'}).status_code == 403
    assert 'secret that must not be logged' not in caplog.text
    # Forwarded headers are not trusted by Flask itself.
    assert client.post('/pair-cart', headers={'X-Forwarded-Proto':'https'}).status_code == 400


def test_two_customers_receipt_restart_and_retry():
    root = Reference()
    app = app_for(root)
    client = app.test_client()
    response = call(client, 'post', '/pair-cart', json={'cart_id':'CART-001'})
    assert response.status_code == 200
    sid = response.json['session_id']
    assert call(client, 'post', '/pair-cart', 'customer-b', json={'cart_id':'CART-001'}).status_code == 409
    assert call(client, 'get', f'/sessions/{sid}/checkout', 'customer-b').status_code == 403
    assert runtime(root,sid).process('ADD','Lays').status == 'committed'
    frozen = call(client,'post',f'/sessions/{sid}/checkout',json={},headers={'Idempotency-Key':'freeze'})
    assert frozen.status_code == 200
    cid = frozen.json['checkout_id']
    base=f'/sessions/{sid}/checkouts/{cid}'
    first=call(client,'post',base+'/payment',json={},headers={'Idempotency-Key':'pay'})
    assert first.status_code == 200
    # Reconstruct cloud app/provider after order creation, before capture.
    client=app_for(root).test_client()
    retry=call(client,'post',base+'/payment',json={},headers={'Idempotency-Key':'pay'})
    assert retry.status_code == 200
    assert retry.json['payment_attempt_id']==first.json['payment_attempt_id']
    done=call(client,'post','/demo'+base+'/simulate-payment-success',json={})
    assert done.status_code == 200
    assert done.json['state']=='COMPLETED'
    again=call(app_for(root).test_client(),'post','/demo'+base+'/simulate-payment-success',json={})
    assert again.json['payment_attempt_id']==done.json['payment_attempt_id']
    receipt=call(client,'get',f'/sessions/{sid}/checkout').json
    assert receipt['items'][0]['total_paise']==1000
    second=call(client,'post','/pair-cart','customer-b',json={'cart_id':'CART-001'})
    assert second.status_code == 200
    assert second.json['session_id'] != sid
    assert call(client,'get',f"/sessions/{second.json['session_id']}/checkout",'customer-b').json['items']==[]
    assert root.data['sessions'][str(sid)]['end_time']


def pending():
    conn=get_connection()
    try:return conn.execute('SELECT session_id,revision FROM cloud_sync_outbox').fetchall()
    finally:conn.close()


@pytest.mark.parametrize('failure',['before','after'])
def test_offline_projection_retries_without_rebilling(failure):
    install()
    root=Reference()
    sid=pair(root)
    assert runtime(root,sid).process('ADD','Lays').status=='committed'
    root.failure=failure
    with pytest.raises(Exception): Publisher(root).flush()
    assert pending()
    root.failure=None
    Publisher(root).flush()
    assert pending()==[]
    assert read_checkout_state(sid,'customer-a')['items'][0]['quantity']==1
    assert root.data['sessions'][str(sid)]['total_amount']==10


def test_outbox_does_not_ack_newer_mutation():
    install()
    root=Reference()
    sid=pair(root)
    assert runtime(root,sid).process('ADD','Lays').status=='committed'
    product=read_checkout_state(sid,'customer-a')['items'][0]['product_id']
    def newer():
        root.before_write=None
        add_item(sid, product)
    root.before_write=newer
    publisher=Publisher(root)
    publisher.flush()
    assert pending()
    publisher.flush()
    assert not pending()
    assert root.data['sessions'][str(sid)]['total_amount']==20


def test_request_validation_and_disabled_edge_ingress():
    client=app_for(Reference()).test_client()
    assert call(client,'post','/pair-cart',json={'cart_id':'CART-001','customer_uid':'victim'}).status_code==400
    assert call(client,'post','/pair-cart',data='x'*20000,headers={'Content-Type':'application/json'}).status_code==413
    assert call(client,'post','/edge/events',json={}).status_code==404


def test_cloud_startup_refuses_missing_database(tmp_path, monkeypatch):
    from edge.cloud.run import main
    monkeypatch.setenv('SMARTCART_DATABASE_PATH', str(tmp_path/'missing.db'))
    with pytest.raises(RuntimeError, match='refusing an empty replacement'):
        main()
    assert not (tmp_path/'missing.db').exists()


def test_stale_projection_backlog_fails_readiness():
    root=Reference()
    client=app_for(root).test_client()
    sid=pair(root)
    conn=get_connection()
    try:
        with conn:
            conn.execute("UPDATE cloud_sync_outbox SET queued_at=datetime('now','-10 minutes')")
    finally:conn.close()
    assert client.get('/health/ready').status_code==503
    Publisher(root).flush()
    assert client.get('/health/ready').status_code==200
