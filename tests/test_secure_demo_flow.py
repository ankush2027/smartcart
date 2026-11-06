"""Full secured HTTP lifecycle with isolated SQLite and in-memory Firebase."""
import pytest
from edge.cloud.app import create_cloud_app
from edge.cloud.outbox import Publisher, install
from edge.cloud.preview import RateLimits
from edge.cloud.software import create_software_supervisor
from tests.test_demo_software_integration import isolated, Reference
from tests.test_preview_security import call


@pytest.fixture(autouse=True)
def isolate_database_for_secure_demo_tests(isolated):
    """Keep this test's outbox/schema writes out of the operator's database."""


def test_secure_demo_two_customers_and_receipt():
    install()
    root = Reference()
    publisher = Publisher(root)
    client = create_cloud_app(verifier=lambda token: {'uid': token},
        publisher=publisher, preview_uids={'a', 'b'}, demo=True,
        demo_allowed_uids={'a', 'b'}, rate_limits=RateLimits(user_burst=100)).test_client()
    assert call(client, token='outsider').status_code == 403
    sid = call(client).json['session_id']
    supervisor, console = create_software_supervisor(publisher)
    runtime = supervisor.runtime_factory(sid)
    assert runtime.process('ADD', 'Lays').status == 'committed'
    assert runtime.process('REMOVE', 'Lays').status == 'committed'
    assert runtime.process('ADD', 'Lays').status == 'committed'
    assert root.data['sessions'][str(sid)]['total_amount'] == 10
    assert call(client, f'/sessions/{sid}/checkout', token='b', method='get').status_code == 403
    checkout = call(client, f'/sessions/{sid}/checkout', headers={'Idempotency-Key': 'checkout-a'})
    assert checkout.status_code == 200, checkout.json
    cid = checkout.json['checkout_id']
    path = f'/sessions/{sid}/checkouts/{cid}'
    payment = call(client, path + '/payment', headers={'Idempotency-Key': 'pay-a'})
    assert payment.status_code == 200, payment.json
    completed = call(client, f'/demo{path}/simulate-payment-success')
    assert completed.status_code == 200, completed.json
    assert completed.json['state'] == 'COMPLETED'
    assert root.data['sessions'][str(sid)]['end_time']
    receipt = call(client, f'/sessions/{sid}/checkout', method='get')
    assert receipt.status_code == 200
    assert receipt.json['state'] == 'COMPLETED'
    assert receipt.json['items'][0]['quantity'] == 1
    next_sid = call(client, token='b').json['session_id']
    assert next_sid > sid
    next_runtime = supervisor.runtime_factory(next_sid)
    assert next_runtime.process('ADD', 'Lays').status == 'committed'
    assert root.data['sessions'][str(next_sid)]['customer_uid'] == 'b'


def test_software_demo_requires_preview_membership(tmp_path):
    import pytest
    from tests.test_local_backend import local_fixture
    from edge.cloud.local import local_environment
    from edge.cloud.run import validate_configuration
    config = local_environment(local_fixture(tmp_path), demo_uids=['a'])
    config['SMARTCART_SOFTWARE_INPUT'] = 'true'
    config['SMARTCART_PREVIEW_UIDS'] = '[]'
    with pytest.raises(RuntimeError, match='preview-approved'):
        validate_configuration(config)
    config['SMARTCART_PREVIEW_UIDS'] = '["a"]'
    validate_configuration(config)
    config['SMARTCART_ALLOWED_CARTS'] = 'CART-002'
    with pytest.raises(RuntimeError, match='CART-001'):
        validate_configuration(config)
