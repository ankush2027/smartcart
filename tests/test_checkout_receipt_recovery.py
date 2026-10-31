from edge.billing.checkout import begin_checkout, read_checkout_state
from edge.demo.composition import create_demo_payment_service, simulate_demo_payment_success
from edge.payment.fake import FakeProvider
from tests.test_demo_software_integration import isolated, Reference, pair, runtime


def test_receipt_recovery_and_completed_retry_without_provider_ledger():
    root = Reference()
    sid = pair(root)
    assert runtime(root, sid).process('ADD', 'Lays').status == 'committed'
    checkout = begin_checkout(sid, 'customer-a', 'freeze')
    recovered = read_checkout_state(sid, 'customer-a')
    assert recovered['state'] == 'CHECKOUT_FROZEN'
    assert recovered['latest_checkout']['checkout_id'] == checkout.checkout_id
    assert recovered['items'][0]['name'] == 'Lays American Style Cream & Onion'
    assert recovered['items'][0]['quantity'] == 1
    service, provider = create_demo_payment_service(firebase_ref=root)
    service.start(sid, checkout.checkout_id, 'customer-a', 'pay')
    result = simulate_demo_payment_success(service, provider, sid, checkout.checkout_id, 'customer-a')
    assert result['state'] == 'COMPLETED'
    # A lost HTTP response can be retried even after the process-local fake ledger is gone.
    retried = simulate_demo_payment_success(service, FakeProvider(), sid, checkout.checkout_id, 'customer-a')
    assert retried['payment_attempt_id'] == result['payment_attempt_id']
    assert retried['projection_sync'] == 'succeeded'
    assert root.data['sessions'][str(sid)]['end_time']
    final = read_checkout_state(sid, 'customer-a')
    assert final['state'] == 'COMPLETED'
    assert final['items'] == recovered['items']
    next_sid = pair(root, owner='customer-b')
    assert next_sid != sid
    assert read_checkout_state(next_sid, 'customer-b')['items'] == []
