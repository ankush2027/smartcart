"""Offline reproduction of reset projections and the actual demo input path."""
from copy import deepcopy
from io import StringIO
import threading
import time

import pytest

from edge.billing import billing
from edge.billing.checkout import CheckoutError, begin_checkout, cancel_checkout
from edge.billing.session_projection import read_session_projection
from edge.database import database
from edge.demo.composition import create_demo_payment_service, simulate_demo_payment_success
from edge.demo.prepare_demo_db import prepare_demo_database
from edge.demo.reconcile import DemoReconciliationError, reconcile_demo_cart
from edge.demo.software_input import SoftwareConsole, SoftwareDemoRuntime
from edge.firebase.reconcile import reconcile_session_to_firebase
from edge.pairing.pair_cart import pair_customer_to_cart
from tests.test_session_reconciliation import StatefulReference


class Reference(StatefulReference):
    def child(self, path):
        return Reference(root=self.root, path=self.path + tuple(path.split('/')))

    def get(self):
        node = self.root.data
        for key in self.path:
            node = node.get(key, {})
        return deepcopy(node)

    def transaction(self, callback):
        if self.root.before_write:
            self.root.before_write()
        if self.root.failure == 'before':
            raise OSError('offline')
        self._replace(self.path, callback(self.get()))
        if self.root.failure == 'after':
            raise OSError('response lost')

    def push(self, value):
        node = self.get()
        self.child('event-' + str(len(node))).set(value)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Deliberately leave the import-time path pointing elsewhere: this reproduces
    # run_demo configuring the database after importing payment reconciliation.
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'wrong.db')
    path = tmp_path / 'demo.db'
    prepare_demo_database(path)
    monkeypatch.setenv('SMARTCART_DATABASE_PATH', str(path))
    monkeypatch.setenv('SMARTCART_ALLOWED_CARTS', 'CART-001,CART-002')
    from edge.firebase import client
    monkeypatch.setattr(client, 'initialize', lambda: pytest.fail('Live Firebase access'))
    monkeypatch.setattr(client, 'get_reference', lambda *a: pytest.fail('Live Firebase access'))


def pair(root, owner='customer-a', cart='CART-001'):
    return pair_customer_to_cart(owner, cart, _firebase_sync_fn=lambda sid, *_:
                                 reconcile_session_to_firebase(sid, _ref=root)).session_id


def runtime(root, sid, console=None):
    return SoftwareDemoRuntime(cart_id='CART-001', session_id=sid, firebase_ref=root,
                               console=console or SoftwareConsole(output=lambda _: None))


def orphan(cart='CART-001', completed=False):
    row = dict(cart_id=cart, customer_uid='customer-a', start_time='2026-01-01 00:00:00',
               total_amount=10, bill_items={'1': {'quantity': 1}})
    if completed:
        row['end_time'] = '2026-01-01 01:00:00'
    return row


def test_reset_orphan_reproduces_not_found_then_scoped_repair_prevents_id_reuse():
    root = Reference({'sessions': {'1': orphan(), '12': orphan(completed=True),
                                   '90': orphan('CART-002')}})
    assert root.data['sessions']['1'].get('end_time') is None
    with pytest.raises(CheckoutError) as error:
        begin_checkout(1, 'customer-a', request_key='old-checkout')
    assert error.value.code == 'session_not_found'
    other = deepcopy(root.data['sessions']['90'])
    assert reconcile_demo_cart('CART-001', root=root) == (1, 0)
    assert '1' not in root.data['sessions']
    assert root.data['sessions']['90'] == other
    assert root.data['sessions']['12']['end_time']
    sid = pair(root)
    assert sid > 90
    assert 'bill_items' not in root.data['sessions'][str(sid)]
    assert reconcile_demo_cart('CART-001', root=root) == (0, 1)


def test_repair_rebuilds_bill_and_completion_from_selected_database():
    root = Reference()
    sid = pair(root)
    assert runtime(root, sid).process('ADD', 'Lays').status == 'committed'
    original_logs = deepcopy(root.data['sessions'][str(sid)]['event_log'])
    root.data['sessions'][str(sid)]['total_amount'] = 999
    reconcile_demo_cart('CART-001', root=root)
    assert root.data['sessions'][str(sid)]['total_amount'] == 10
    assert root.data['sessions'][str(sid)]['event_log'] == original_logs
    assert read_session_projection(sid).total_amount == 10
    assert not database.DATABASE_PATH.exists()


@pytest.mark.parametrize('field,value', [('customer_uid', 'other'), ('cart_id', 'CART-002'),
                                        ('start_time', '2000-01-01 00:00:00')])
def test_identity_collision_fails_closed(field, value):
    root = Reference()
    sid = pair(root)
    root.data['sessions'][str(sid)][field] = value
    before = deepcopy(root.data)
    with pytest.raises(DemoReconciliationError, match='collision'):
        reconcile_demo_cart('CART-001', root=root)
    assert root.data == before


def test_concurrent_projection_change_stops_repair():
    root = Reference({'sessions': {'20': orphan()}})
    root.before_write = lambda: root.data['sessions']['20'].update(total_amount=42)
    with pytest.raises(DemoReconciliationError):
        reconcile_demo_cart('CART-001', root=root)
    assert root.data['sessions']['20']['total_amount'] == 42


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_uncertain_repair_can_be_repeated_without_reusing_ids(failure):
    root = Reference({'sessions': {'20': orphan()}})
    root.failure = failure
    with pytest.raises(DemoReconciliationError):
        reconcile_demo_cart('CART-001', root=root)
    root.failure = None
    reconcile_demo_cart('CART-001', root=root)
    assert pair(root) > 20


def test_rtdb_array_representation_supported():
    root = Reference({'sessions': [None, orphan(), None, orphan('CART-002')]})
    reconcile_demo_cart('CART-001', root=root)
    assert set(root.data['sessions']) == {'3'}
    assert pair(root) > 3


@pytest.mark.parametrize('data', ['bad', {'bogus': orphan()}, {'01': orphan()},
                                   {'1': 'bad'}, {'9223372036854775807': orphan()}])
def test_invalid_projection_stops_startup(data):
    root = Reference({'sessions': data})
    with pytest.raises(DemoReconciliationError):
        reconcile_demo_cart('CART-001', root=root)


def test_actual_software_input_checkout_cancel_payment_next_customer():
    root = Reference()
    sid = pair(root)
    old = runtime(root, sid)
    assert old.process('ADD', 'Lays').status == 'committed'
    assert old.process('REMOVE', 'Lays').status == 'committed'
    assert billing.get_bill(sid) == []
    assert old.process('ADD', 'Lays').status == 'committed'
    checkout = begin_checkout(sid, 'customer-a', request_key='freeze-1')
    cancel_checkout(sid, checkout.checkout_id, 'customer-a')
    assert old.process('ADD', 'Lays').status == 'rejected'
    fresh = runtime(root, sid)
    assert fresh.generation > old.generation
    assert fresh.process('ADD', 'Coca Cola').status == 'committed'
    final = begin_checkout(sid, 'customer-a', request_key='freeze-2')
    assert fresh.process('ADD', 'Lays').status == 'rejected'
    service, provider = create_demo_payment_service(firebase_ref=root)
    service.start(sid, final.checkout_id, 'customer-a', request_key='payment')
    result = simulate_demo_payment_success(service, provider, sid, final.checkout_id, 'customer-a')
    assert result['state'] == 'COMPLETED'
    assert root.data['sessions'][str(sid)]['end_time'] == billing.get_session_meta(sid)['end_time']
    next_sid = pair(root, owner='customer-b')
    assert next_sid > sid
    next_runtime = runtime(root, next_sid)
    assert billing.get_bill(next_sid) == []
    assert next_runtime.process('ADD', 'Lays').status == 'committed'
    assert root.data['sessions'][str(next_sid)]['customer_uid'] == 'customer-b'
    assert root.data['sessions'][str(next_sid)]['total_amount'] == 10


def test_console_windows_portable_read_and_session_generation_fences():
    root = Reference()
    sid = pair(root)
    console = SoftwareConsole(output=lambda _: None)
    assert not console.submit('ADD 1 0 Lays')
    worker = runtime(root, sid, console)
    console.bind(worker)
    assert not console.submit(f'ADD {sid+1} {worker.generation} Lays')
    assert not console.submit(f'ADD {sid} {worker.generation+1} Lays')
    assert not console.submit(f'ADD {sid} {worker.generation} Bru')  # Below existing threshold.
    assert not console.submit('malformed "')
    console.read(StringIO(f'STATUS\nADD {sid} {worker.generation} "Lays"\n'))
    assert worker.commands.get_nowait() == ('ADD', 'Lays')
    worker.stop()
    assert not console.submit(f'ADD {sid} {worker.generation} Lays')
    console.unbind(worker)


def test_worker_stops_without_transferring_queued_events():
    root = Reference()
    sid = pair(root)
    worker = runtime(root, sid)
    worker.commands.put(('ADD', 'Lays'))
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not billing.get_bill(sid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert billing.get_bill(sid)
    finally:
        worker.stop()
        thread.join(3)
    assert not thread.is_alive()
    assert worker.console._runtime is None


def test_projection_failure_keeps_sqlite_mutation_and_stops_input():
    root = Reference()
    sid = pair(root)
    worker = runtime(root, sid)
    root.failure = 'before'
    assert worker.process('ADD', 'Lays').status == 'committed_sync_failed'
    assert billing.get_session_meta(sid)['total_amount'] == 10
    assert worker.stopping.is_set()


def test_real_supervisor_drains_cancelled_runtime_and_releases_for_next_customer():
    from edge.runtime.supervisor import CartSupervisor
    root = Reference()
    console = SoftwareConsole(output=lambda _: None)
    attached = []

    def factory(sid):
        if attached:
            assert attached[-1].stopping.is_set()
            assert console._runtime is None
        worker = runtime(root, sid, console)
        attached.append(worker)
        return worker

    def until(check):
        deadline = time.monotonic() + 5
        while not check() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert check()

    supervisor = CartSupervisor(cart_id='CART-001', runtime_factory=factory,
                                poll_interval=0.01, drain_poll_interval=0.01)
    thread = threading.Thread(target=supervisor.run_forever)
    thread.start()
    try:
        sid = pair(root)
        until(lambda: console._runtime is not None)
        old = attached[-1]
        assert console.submit(f'ADD {sid} {old.generation} Lays')
        until(lambda: root.data['sessions'][str(sid)]['total_amount'] == 10)
        frozen = begin_checkout(sid, 'customer-a', request_key='supervised-1')
        until(lambda: console._runtime is None)
        assert not console.submit(f'ADD {sid} {old.generation} Lays')
        cancel_checkout(sid, frozen.checkout_id, 'customer-a')
        until(lambda: len(attached) == 2 and console._runtime is attached[-1])
        fresh = attached[-1]
        assert fresh.generation > old.generation
        assert not console.submit(f'ADD {sid} {old.generation} Lays')
        assert console.submit(f'ADD {sid} {fresh.generation} Lays')
        until(lambda: root.data['sessions'][str(sid)]['total_amount'] == 20)
        final = begin_checkout(sid, 'customer-a', request_key='supervised-2')
        until(lambda: console._runtime is None)
        service, provider = create_demo_payment_service(firebase_ref=root)
        service.start(sid, final.checkout_id, 'customer-a', request_key='pay')
        assert simulate_demo_payment_success(service, provider, sid, final.checkout_id,
                                             'customer-a')['state'] == 'COMPLETED'
        next_sid = pair(root, owner='customer-b')
        until(lambda: len(attached) == 3 and console._runtime is attached[-1])
        assert attached[-1].session_id == next_sid
        assert billing.get_bill(next_sid) == []
    finally:
        supervisor.request_stop()
        thread.join(5)
    assert not thread.is_alive()
    assert console._runtime is None


@pytest.mark.parametrize('repair_fails', [False, True])
def test_entrypoint_repairs_before_serving_and_selects_software_runtime(monkeypatch, repair_fails):
    from types import SimpleNamespace
    from edge.demo import run_demo
    from edge.firebase import client
    import waitress
    root = Reference({'sessions': {'42': orphan()}})
    root.failure = 'before' if repair_fails else None
    monkeypatch.setattr(client, 'initialize', lambda: None)
    monkeypatch.setattr(client, 'get_reference', lambda _: root)
    factory = []
    served = []

    def supervise(**kwargs):
        factory.append(kwargs['runtime_factory'])
        return SimpleNamespace(run_forever=lambda: None, request_stop=lambda: None)

    monkeypatch.setattr(run_demo, 'CartSupervisor', supervise)
    monkeypatch.setattr(run_demo, 'threading', SimpleNamespace(Thread=lambda **kw:
                        SimpleNamespace(start=lambda: None, join=lambda **kw: None)))

    def serve(app, **kwargs):
        served.append(True)
        assert '42' not in root.data.get('sessions', {})
        sid = pair(root)
        assert sid > 42
        assert isinstance(factory[0](sid), SoftwareDemoRuntime)
        assert app.test_client().post('/pair-cart', json={'cart_id': 'CART-001'}).status_code == 401

    monkeypatch.setattr(waitress, 'serve', serve)
    result = run_demo.main(['--db-path', str(database.get_database_path()),
                            '--reconcile-demo-projections', '--software-input', '--with-supervisor'])
    assert result == (1 if repair_fails else 0)
    assert bool(served) is not repair_fails
    assert bool(factory) is not repair_fails
