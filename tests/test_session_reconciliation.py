"""Controlled recovery with real temporary SQLite and stateful offline RTDB."""
from contextlib import closing
from copy import deepcopy
import sqlite3

import pytest

from edge.billing import billing, session_projection
from edge.billing.session_projection import ProjectionError, read_session_projection
from edge.cart_session import CartSession
from edge.database import database
from edge.database.product_repository import add_product, get_all_products
from edge.detection.product_resolver import ProductResolver
from edge.firebase import client, run_reconcile
from edge.firebase.reconcile import ReconciliationError, reconcile_session_to_firebase


class StatefulReference:
    """Models RTDB child replacement and null deletion, including lost replies."""
    def __init__(self, data=None, *, root=None, path=()):
        self.root = root if root is not None else self
        self.path = path
        if root is None:
            self.data = deepcopy(data or {})
            self.calls = []
            self.failure = None
            self.before_write = None

    def child(self, path):
        return StatefulReference(root=self.root, path=self.path + tuple(path.split('/')))

    def _replace(self, path, value):
        node = self.root.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = deepcopy(value)

    def update(self, values):
        self.root.calls.append(('update', self.path, deepcopy(values)))
        if self.root.before_write:
            self.root.before_write()
        if self.root.failure == 'before':
            raise OSError('offline')
        for key, value in values.items():
            self._replace(self.path + tuple(key.split('/')), value)
        if self.root.failure == 'after':
            raise OSError('applied write, response lost')

    def set(self, value):
        self.root.calls.append(('set', self.path, deepcopy(value)))
        self._replace(self.path, value)

    def delete(self):
        self.root.calls.append(('delete', self.path))
        self._replace(self.path, None)

    def push(self, value):
        self.root.calls.append(('push', self.path, deepcopy(value)))
        raise AssertionError('Recovery must never push events')


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'recovery.db')
    database.initialize_database()
    monkeypatch.setattr(client, 'initialize', lambda: pytest.fail('Live Firebase initialization'))
    monkeypatch.setattr(client, 'get_reference', lambda *a: pytest.fail('Live Firebase reference'))


@pytest.fixture
def populated():
    add_product('Snack', 'a', 10, 75, 'Snack')
    add_product('Drink', 'b', 20, 150, 'Drink')
    products = get_all_products()
    sid = billing.create_session('CART-001', customer_uid='uid-owner')
    for product in products:
        assert billing.add_item(sid, product[0])
    return sid, [p[0] for p in products]


def source_state():
    with closing(database.get_connection()) as conn:
        logical = tuple(conn.iterdump())
    return database.DATABASE_PATH.read_bytes(), logical


def cloud_session(root, sid):
    return root.data['sessions'][str(sid)]


def expected(sid):
    meta = billing.get_session_meta(sid)
    result = {key: value for key, value in meta.items() if key != 'session_id' and value is not None}
    bill = {str(pid): dict(product_name=name, quantity=qty, price=price, total=total)
            for pid, name, qty, price, total in billing.get_bill(sid)}
    if bill:
        result['bill_items'] = bill
    return result


def forbid_mutations(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Recovery invoked a billing/lifecycle mutation')
    for name in ('add_item', 'remove_item', 'create_session', 'complete_session',
                 'assign_customer_to_session', 'set_session_cart_id', '_update_total'):
        monkeypatch.setattr(billing, name, forbidden)
    for name in ('process_add', 'process_remove', 'complete', 'assign_customer'):
        monkeypatch.setattr(CartSession, name, forbidden)


def test_reconcile_missing_active_projection_without_mutations(populated, monkeypatch):
    sid, _ = populated
    before = source_state()
    root = StatefulReference()
    forbid_mutations(monkeypatch)
    result = reconcile_session_to_firebase(sid, _ref=root)
    assert (result.session_id, result.bill_item_count, result.completed) == (sid, 2, False)
    assert cloud_session(root, sid) == expected(sid)
    assert len(root.calls) == 1 and root.calls[0][:2] == ('update', ('sessions', str(sid)))
    assert source_state() == before


def test_replace_stale_projection_preserve_events_children_and_other_sessions(populated):
    sid, pids = populated
    events = {'push-original': {'timestamp': 'old', 'event_type': 'ADD', 'product_id': pids[0]}}
    root = StatefulReference({'sessions': {str(sid): {
        'customer_uid': 'wrong', 'cart_id': 'wrong', 'start_time': 'old', 'end_time': 'old',
        'total_amount': 999, 'event_log': events, 'unrelated_child': {'keep': True},
        'bill_items': {str(pids[0]): {'product_name': 'wrong', 'quantity': 99, 'price': 8, 'total': 792},
                       '999999': {'product_name': 'extra'}}},
        'other': {'bill_items': {'x': {'quantity': 7}}, 'event_log': {'untouched': True}}}})
    before, other = source_state(), deepcopy(root.data['sessions']['other'])
    for _ in range(2):
        reconcile_session_to_firebase(sid, _ref=root)
        assert cloud_session(root, sid) == {**expected(sid), 'event_log': events,
                                            'unrelated_child': {'keep': True}}
        assert root.data['sessions']['other'] == other
        assert source_state() == before
    assert len(root.calls) == 2 and all(call[0] == 'update' for call in root.calls)


@pytest.mark.parametrize('completed', [False, True])
def test_empty_bill_removes_stale_subtree(completed):
    sid = billing.create_session()  # Nullable cart/owner remain supported.
    if completed:
        billing.complete_session(sid)
    root = StatefulReference({'sessions': {str(sid): {'bill_items': {'1': {'quantity': 3}},
                                                     'customer_uid': 'stale', 'cart_id': 'stale',
                                                     'event_log': {'keep': 1}}}})
    before = source_state()
    result = reconcile_session_to_firebase(sid, _ref=root)
    assert result.completed == completed
    assert cloud_session(root, sid) == {**expected(sid), 'event_log': {'keep': 1}}
    assert root.calls[0][2]['bill_items'] is None
    assert source_state() == before


@pytest.mark.parametrize('sid', [0, -1, True, 1.0, '1', None, 2**63, '../other'])
def test_invalid_id_never_contacts_firebase(sid):
    before = source_state()
    with pytest.raises(ProjectionError):
        reconcile_session_to_firebase(sid)
    assert source_state() == before


def test_missing_session_never_contacts_firebase():
    before = source_state()
    with pytest.raises(ProjectionError, match='does not exist'):
        reconcile_session_to_firebase(123)
    assert source_state() == before


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / 'absent.db'
    monkeypatch.setattr(database, 'DATABASE_PATH', missing)
    with pytest.raises(sqlite3.OperationalError):
        reconcile_session_to_firebase(1)
    assert not missing.exists()


@pytest.mark.parametrize('corruption', [
    'DELETE FROM products WHERE product_id = 1',
    "UPDATE products SET name = '' WHERE product_id = 1",
    'UPDATE bill_items SET quantity = 0 WHERE product_id = 1',
    'UPDATE bill_items SET quantity = 1.5 WHERE product_id = 1',
    'UPDATE bill_items SET price = -1 WHERE product_id = 1',
    'UPDATE bill_items SET total = 77 WHERE product_id = 1',
    'UPDATE sessions SET total_amount = 500',
    'UPDATE sessions SET total_amount = NULL',
    'UPDATE sessions SET total_amount = 1e999',
    "UPDATE sessions SET customer_uid = ''",
    "UPDATE sessions SET start_time = 'invalid'",
    "UPDATE sessions SET end_time = 'invalid'",
    "UPDATE sessions SET end_time = '2000-01-01 00:00:00'",
    'INSERT INTO bill_items (session_id, product_id, quantity, price, total) '
    'SELECT session_id, product_id, quantity, price, total FROM bill_items WHERE product_id = 1',
])
def test_corrupt_source_fails_before_any_firebase_access(populated, corruption):
    sid, _ = populated
    with closing(database.get_connection()) as conn:
        conn.execute(corruption)
        conn.commit()
    before = source_state()
    with pytest.raises(ProjectionError):
        reconcile_session_to_firebase(sid)
    assert source_state() == before


def test_snapshot_consistency_across_concurrent_commit(populated, monkeypatch):
    sid, pids = populated
    with closing(database.get_connection()) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    original_connect = sqlite3.connect
    readers, statements = [], []
    committed = False

    def connect(*args, **kwargs):
        nonlocal committed
        conn = original_connect(*args, **kwargs)
        if kwargs.get('uri'):
            assert 'mode=ro' in args[0]
            readers.append(conn)

            def trace(sql):
                nonlocal committed
                statements.append(sql)
                if sql.startswith('SELECT b.product_id') and not committed:
                    committed = True
                    assert billing.add_item(sid, pids[0])  # Commits between the two reads.
            conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(session_projection.sqlite3, 'connect', connect)
    snapshot = read_session_projection(sid)
    assert committed and len(readers) == 1
    assert statements[0] == 'BEGIN'
    assert all(sql == 'BEGIN' or sql.startswith('SELECT') for sql in statements)
    assert snapshot.total_amount == 30
    assert [item.quantity for item in snapshot.bill_items] == [1, 1]
    assert billing.get_session_meta(sid)['total_amount'] == 40
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        readers[0].execute('SELECT 1')


def test_read_transaction_released_before_firebase_write(populated):
    sid, _ = populated
    root = StatefulReference()

    def prove_unlocked():
        with closing(database.get_connection()) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN EXCLUSIVE')  # Fails if any rollback-journal reader remains.
            conn.rollback()
    root.before_write = prove_unlocked
    reconcile_session_to_firebase(sid, _ref=root)


def test_completion_sync_failure_repaired_without_recompletion(populated, monkeypatch):
    sid, _ = populated
    root = StatefulReference()
    reconcile_session_to_firebase(sid, _ref=root)
    session = CartSession(ProductResolver([]), session_id=sid, firebase_ref=root)
    root.failure = 'before'
    with pytest.raises(OSError):
        session.complete()
    assert billing.get_session_meta(sid)['end_time'] is not None
    assert 'end_time' not in cloud_session(root, sid)
    with pytest.raises(billing.InactiveSessionError):
        session.complete()
    before = source_state()
    forbid_mutations(monkeypatch)
    root.failure = None
    result = reconcile_session_to_firebase(sid, _ref=root)
    assert result.completed
    assert cloud_session(root, sid) == expected(sid)
    assert source_state() == before


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_remote_failure_and_controlled_rerun(populated, failure):
    sid, _ = populated
    root = StatefulReference({'sessions': {str(sid): {'event_log': {'original': {'keep': True}},
                                                     'total_amount': 999}}})
    before, cloud_before = source_state(), deepcopy(root.data)
    root.failure = failure
    with pytest.raises(ReconciliationError, match='uncertain'):
        reconcile_session_to_firebase(sid, _ref=root)
    assert source_state() == before
    assert len(root.calls) == 1  # No application retry.
    if failure == 'before':
        assert root.data == cloud_before
    else:
        assert cloud_session(root, sid)['total_amount'] == 30
    root.failure = None
    reconcile_session_to_firebase(sid, _ref=root)
    first = deepcopy(root.data)
    reconcile_session_to_firebase(sid, _ref=root)
    assert root.data == first
    assert cloud_session(root, sid) == {**expected(sid), 'event_log': {'original': {'keep': True}}}
    assert all(call[0] == 'update' for call in root.calls)
    assert source_state() == before


def test_cli_uses_backend_configuration_after_snapshot(populated, monkeypatch, capsys):
    sid, _ = populated
    root, initialized = StatefulReference(), []
    monkeypatch.setattr(client, 'initialize', lambda: initialized.append(True))
    monkeypatch.setattr(client, 'get_reference', lambda path: root)
    assert run_reconcile.main([str(sid)]) == 0
    captured = capsys.readouterr()
    assert initialized == [True]
    assert f'Reconciled session {sid}' in captured.out
    assert 'quiesced' in captured.err
    assert cloud_session(root, sid) == expected(sid)


def test_controlled_rerun_reads_fresh_state_after_lost_response(populated):
    sid, pids = populated
    root = StatefulReference({'sessions': {str(sid): {'event_log': {'preserved': True}}}})
    root.failure = 'after'
    with pytest.raises(ReconciliationError):
        reconcile_session_to_firebase(sid, _ref=root)
    # A separate trusted operation commits before a later, again-quiesced repair.
    billing.remove_item(sid, pids[0])
    before = source_state()
    root.failure = None
    reconcile_session_to_firebase(sid, _ref=root)
    assert cloud_session(root, sid) == {**expected(sid), 'event_log': {'preserved': True}}
    assert str(pids[0]) not in cloud_session(root, sid)['bill_items']
    assert source_state() == before


@pytest.mark.parametrize('args', [[], ['0'], ['abc'], ['1', '--customer-uid', 'other'],
                                 ['1', '--path', '/'], ['1', '--payload', '{}']])
def test_cli_rejects_invalid_arguments_without_firebase(args):
    with pytest.raises(SystemExit) as error:
        run_reconcile.main(args)
    assert error.value.code == 2


def test_cli_source_failure_has_nonzero_status_and_no_firebase(capsys):
    assert run_reconcile.main(['999']) == 1
    assert 'No Firebase write attempted' in capsys.readouterr().err


def test_cli_remote_failure_does_not_print_secrets(populated, monkeypatch, capsys):
    sid, _ = populated
    def fail():
        raise OSError('SECRET credential detail')
    monkeypatch.setattr(client, 'initialize', fail)
    assert run_reconcile.main([str(sid)]) == 1
    captured = capsys.readouterr()
    assert 'uncertain' in captured.err
    assert 'SECRET' not in captured.err + captured.out
