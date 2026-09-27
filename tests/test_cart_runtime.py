"""Software runtime tests: real orchestration/resolver/SQLite, fake I/O only."""
from datetime import datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from edge.database import database
from edge.database.product_repository import add_product, get_all_products
from edge.billing import billing
from edge.cart_session import CartSession
from edge.detection.event import DetectionEvent
from edge.detection.mog2 import MOG2Processor
from edge.detection.product_resolver import Detection, ProductResolver
from edge.runtime.cart_runtime import CartRuntime, StartupError, resume_session, validated_catalog, validate_model_path
from edge.runtime.run_cart import main, cart_lock
from edge.runtime.weight_source import SimulatedWeightSource
from edge.pairing.session_store import IntegrityConflict
from tests.test_runtime_firebase_sync import _FakeReference, _ErrorReference


class Detector:
    class_names = ('Snack',)
    def __init__(self):
        self.detections = [Detection('Snack', .9)]
        self.calls = []
    def detect(self, frame):
        self.calls.append(frame)
        return self.detections


class Motion:
    event = None
    def process(self, frame, timestamp=None):
        result, self.event = self.event, None
        return result


class Weights(SimulatedWeightSource):
    event = None
    closed = False
    def poll(self):
        result, self.event = self.event, None
        return result
    def close(self):
        self.closed = True


class Camera:
    released = False
    def read(self):
        return False, None
    def release(self):
        self.released = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'cart.db')
    database.initialize_database()
    add_product('Snack', 'sku', 10, 75, 'Snack')
    sid = billing.create_session(cart_id='CART-A')
    return sid, get_all_products()[0][0]


def runtime(ref=None):
    detector = Detector()
    session = resume_session('CART-A', detector, ref)
    return CartRuntime(camera=Camera(), motion=Motion(), weights=Weights(),
                       detector=detector, session=session, cart_id='CART-A',
                       clock=lambda: datetime(2026, 1, 1))


def acquire(rt, delta=None, movement=True, offset=0):
    ts = datetime(2026, 1, 1) + timedelta(seconds=offset)
    rt.clock = lambda: ts
    rt.motion.event = DetectionEvent(ts, 'MOVEMENT', (0, 0, 5, 5), 1000) if movement else None
    if delta is not None:
        rt.weights.event = rt.weights.inject_delta(delta)
        rt.weights.event.timestamp = ts
    rt.acquire([1, 2, 3], timestamp=ts)


def dispatch_next(rt):
    item = rt.jobs.get_nowait()
    result = rt.dispatch(item)
    rt.jobs.task_done()
    return result


def test_confirmed_add_remove_once_and_firebase(setup):
    sid, pid = setup
    root = _FakeReference()
    rt = runtime(root)
    acquire(rt, 75)
    assert dispatch_next(rt).status == 'committed'
    assert rt.jobs.empty()
    assert billing.get_bill(sid)[0][2] == 1
    acquire(rt, -75, offset=2)
    assert dispatch_next(rt).status == 'committed'
    assert rt.jobs.empty()
    assert billing.get_bill(sid) == []
    assert [r[3] for r in billing.get_session_log(sid)] == ['ADD', 'REMOVE']
    assert len(rt.detector.calls) == 2
    assert billing.get_session_meta(sid)['total_amount'] == 0


@pytest.mark.parametrize('movement,delta', [(True, None), (False, 75)])
def test_single_channel_never_bills(setup, movement, delta):
    rt = runtime()
    acquire(rt, delta, movement)
    assert rt.jobs.empty()
    assert billing.get_bill(setup[0]) == []
    assert rt.detector.calls == []


def test_weight_before_movement_and_raw_frame_copy(setup):
    rt = runtime()
    acquire(rt, 75, movement=False)
    frame = [4, 5]
    ts = datetime(2026, 1, 1, 0, 0, 0, 500000)
    rt.clock = lambda: ts
    rt.motion.event = DetectionEvent(ts, 'MOVEMENT', (), 1000)
    rt.acquire(frame, ts)
    frame.append(99)
    assert dispatch_next(rt).status == 'committed'
    assert rt.detector.calls == [[4, 5]]


def test_outside_window_never_bills(setup):
    rt = runtime()
    acquire(rt, None)
    acquire(rt, 75, movement=False, offset=2)
    assert rt.jobs.empty()


def test_unresolved_is_not_billed_and_worker_halts(setup):
    rt = runtime()
    rt.detector.detections = [Detection('Unknown', .99)]
    results = []
    rt.on_outcome = results.append
    acquire(rt, 75)
    acquire(rt, 75, offset=2)
    worker = Thread(target=rt._work)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert rt.failed.is_set()
    assert [r.status for r in results] == ['unresolved']
    assert billing.get_bill(setup[0]) == []


def test_restart_hydrates_authoritative_quantities(setup):
    rt = runtime()
    for i in range(2):
        acquire(rt, 75, offset=i * 2)
        assert dispatch_next(rt).status == 'committed'
    restarted = runtime()
    assert restarted.session.cart_state.snapshot()[0].quantity == 2
    acquire(restarted, -75)
    assert dispatch_next(restarted).status == 'committed'
    assert billing.get_bill(setup[0])[0][2] == 1
    assert billing.get_session_meta(setup[0])['total_amount'] == 10


@pytest.mark.parametrize('operation', ['add_item', 'remove_item'])
def test_completed_session_guard_inside_billing_transaction(setup, operation):
    sid, pid = setup
    billing.add_item(sid, pid)
    rt = runtime()
    billing.complete_session(sid)
    before = billing.get_bill(sid)
    with pytest.raises(billing.InactiveSessionError):
        getattr(billing, operation)(sid, pid)
    assert not rt.session.process_add([Detection('Snack', .99)], 75).success
    assert not rt.session.process_remove([], -75).success
    assert billing.get_bill(sid) == before
    with pytest.raises(StartupError, match='No active session'):
        runtime()
    with pytest.raises(billing.InactiveSessionError):
        CartSession(ProductResolver(validated_catalog(('Snack',))), session_id=sid)


def test_ambiguous_active_session_rejected(setup):
    billing.create_session(cart_id='CART-A')
    with pytest.raises(IntegrityConflict):
        runtime()


def test_startup_missing_model_has_no_device_or_network_effect(tmp_path, monkeypatch):
    import edge.runtime.run_cart as cli
    monkeypatch.setattr(cli, 'initialize', lambda: pytest.fail('Firebase called'))
    monkeypatch.setattr(cli, 'initialize_database', lambda: pytest.fail('Database called'))
    missing = tmp_path / 'missing.pt'
    with pytest.raises(StartupError, match='missing'):
        validate_model_path(missing)
    assert main(['--cart-id', 'CART-A', '--simulate-weight', '--model', str(missing)]) == 1


@pytest.mark.parametrize('names', [(), ('Unknown',), ('Snack', 'Snack'), ('Snack', 'Other')])
def test_catalog_model_mismatch(setup, names):
    with pytest.raises(StartupError):
        validated_catalog(names)


def test_duplicate_catalog_class_rejected(setup):
    add_product('Another', 'sku2', 20, 80, 'Snack')
    with pytest.raises(StartupError):
        validated_catalog(('Snack',))


@pytest.mark.parametrize('kind', ['ADD', 'REMOVE'])
def test_firebase_failure_committed_once_no_retry(setup, kind):
    sid, pid = setup
    if kind == 'REMOVE':
        billing.add_item(sid, pid)
    rt = runtime(_ErrorReference())
    results = []
    rt.on_outcome = results.append
    acquire(rt, 75 if kind == 'ADD' else -75)
    acquire(rt, 75, offset=2)  # queued event must not execute after uncertainty
    worker = Thread(target=rt._work)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert [r.status for r in results] == ['committed_sync_failed']
    assert len(billing.get_session_log(sid)) == (1 if kind == 'ADD' else 2)
    assert billing.get_session_meta(sid)['total_amount'] == (10 if kind == 'ADD' else 0)
    assert rt.session.last_mutation_committed


def test_remove_sqlite_exception_restores_memory(setup, monkeypatch):
    import edge.cart_session as module
    billing.add_item(*setup)
    rt = runtime()
    def fail(*_):
        raise OSError('SQLite failed')
    monkeypatch.setattr(module, '_billing_remove', fail)
    acquire(rt, -75)
    assert dispatch_next(rt).status == 'rejected'
    assert rt.session.cart_state.snapshot()[0].quantity == 1
    assert not rt.session.last_mutation_committed


def test_camera_failure_cleans_up_and_joins_worker(setup):
    rt = runtime()
    with pytest.raises(RuntimeError, match='Camera'):
        rt.run()
    assert rt.camera.released and rt.weights.closed
    assert not rt._worker.is_alive()


def test_queue_overflow_is_explicit_fail_stop(setup):
    rt = runtime()
    rt.jobs = __import__('queue').Queue(maxsize=1)
    acquire(rt, 75)
    with pytest.raises(RuntimeError, match='queue full'):
        acquire(rt, 75, offset=2)
    assert rt.failed.is_set()
    assert billing.get_bill(setup[0]) == []


def test_acquisition_continues_while_recognition_blocks(setup):
    rt = runtime()
    entered, release = Event(), Event()
    original = rt.detector.detect
    def blocked(frame):
        entered.set()
        assert release.wait(3)
        return original(frame)
    rt.detector.detect = blocked
    acquire(rt, 75)
    worker = Thread(target=rt._work)
    worker.start()
    try:
        assert entered.wait(3)
        acquire(rt, 75, offset=2)
        assert rt.jobs.qsize() == 1
    finally:
        release.set()
        rt.stop()
        worker.join(3)
    assert not worker.is_alive()
    assert billing.get_bill(setup[0])[0][2] == 2


def test_lock_rejects_second_runtime(tmp_path, monkeypatch):
    import edge.runtime.run_cart as cli
    monkeypatch.setattr(cli, 'DATABASE_PATH', tmp_path / 'locktest.db')
    with cart_lock('CART-A'):
        with pytest.raises(RuntimeError, match='already owns'):
            with cart_lock('CART-A'):
                pass


class CV:
    MORPH_ELLIPSE = MORPH_OPEN = MORPH_CLOSE = RETR_EXTERNAL = CHAIN_APPROX_SIMPLE = 1
    area = 501
    def createBackgroundSubtractorMOG2(self, **kwargs):
        assert kwargs == dict(history=500, varThreshold=50, detectShadows=True)
        return SimpleNamespace(apply=lambda frame: frame)
    def getStructuringElement(self, *args): return 'kernel'
    def morphologyEx(self, mask, *args): return mask
    def findContours(self, *args): return [self.area], None
    def contourArea(self, contour): return contour
    def boundingRect(self, contour): return (1, 2, 3, 4)
    def countNonZero(self, mask): return 1000


def test_mog2_preserves_threshold_gesture_and_unannotated_input():
    cv = CV()
    processor = MOG2Processor(cv=cv)
    frame = [1, 2]
    for _ in range(4):
        assert processor.process(frame) is None
    assert processor.process(frame).event_type == 'MOVEMENT'
    assert processor.process(frame) is None
    cv.area = 500
    assert processor.process(frame) is None
    cv.area = 501
    for _ in range(4):
        assert processor.process(frame) is None
    assert processor.process(frame) is not None
    assert frame == [1, 2]


def test_simulation_threshold_baseline_and_invalid_input():
    source = Weights()
    assert source.inject_delta(5) is None
    assert source.inject_delta(5).weight_change == 10
    assert source.inject_delta(-10).event_type == 'REMOVE'
    for delta in (float('nan'), float('inf'), -1000):
        with pytest.raises(ValueError):
            source.inject_delta(delta)


@pytest.mark.parametrize('sync_failure', [False, True])
def test_duplicate_confirmation_cannot_repeat_even_after_sync_error(setup, sync_failure):
    rt = runtime(_ErrorReference() if sync_failure else None)
    acquire(rt, 75)
    job = rt.jobs.get_nowait()
    assert rt.dispatch(job).status == ('committed_sync_failed' if sync_failure else 'committed')
    assert rt.dispatch(job).status == 'ignored'
    assert len(billing.get_session_log(setup[0])) == 1
    assert billing.get_bill(setup[0])[0][2] == 1
    assert len(rt.detector.calls) == 1


@pytest.mark.parametrize('age', [31, -1])
def test_stale_or_future_event_cannot_bill(setup, age):
    rt = runtime()
    acquire(rt, 75)
    rt.clock = lambda: datetime(2026, 1, 1) + timedelta(seconds=age)
    assert dispatch_next(rt).status == 'rejected'
    assert billing.get_bill(setup[0]) == []
    assert rt.detector.calls == []


def test_old_event_cannot_be_replayed_into_restarted_runtime(setup):
    first = runtime()
    acquire(first, 75)
    job = first.jobs.get_nowait()
    assert first.dispatch(job).status == 'committed'
    second = runtime()
    second.started_at = datetime(2026, 1, 1, 0, 0, 1)
    second.clock = lambda: second.started_at
    assert second.dispatch(job).status == 'rejected'
    assert billing.get_bill(setup[0])[0][2] == 1


def test_reusing_one_channel_of_confirmation_does_not_bill_twice(setup):
    rt = runtime()
    acquire(rt, 75)
    job = rt.jobs.get_nowait()
    assert rt.dispatch(job).status == 'committed'
    job['confirmed']['movement_timestamp'] += timedelta(milliseconds=100)
    rt.clock = lambda: datetime(2026, 1, 1, 0, 0, 1)
    assert rt.dispatch(job).status == 'ignored'
    assert len(billing.get_session_log(setup[0])) == 1


def test_existing_yolo_adapter_in_complete_runtime_loop(setup):
    from edge.model.yolo_detector import YOLODetector
    from tests.test_yolo_detector import _FakeBox, _FakeResult, _FakeModel
    model = _FakeModel(_FakeResult([_FakeBox(0, .95)], {0: 'Snack'}))
    model.names = {0: 'Snack'}
    detector = YOLODetector(_model=model)
    root = _FakeReference()
    rt = runtime(root)
    rt.detector = detector
    rt.session = resume_session('CART-A', detector, root)
    ts = datetime(2026, 1, 1)
    rt.motion.event = DetectionEvent(ts, 'MOVEMENT', (), 1000)
    rt.weights.event = rt.weights.inject_delta(75)
    rt.weights.event.timestamp = ts
    class LoopCamera(Camera):
        def read(self):
            return True, [1, 2, 3]
    rt.camera = LoopCamera()
    outcomes = []
    def finish(result):
        outcomes.append(result)
        rt.stop()
    rt.on_outcome = finish
    thread = Thread(target=rt.run)
    thread.start()
    thread.join(3)
    if thread.is_alive():
        rt.stop()
        thread.join(3)
        pytest.fail('Runtime failed to stop')
    assert [r.status for r in outcomes] == ['committed']
    assert billing.get_bill(setup[0])[0][2] == 1
    assert len(billing.get_session_log(setup[0])) == 1
    assert rt.camera.released and rt.weights.closed
    assert not rt._worker.is_alive()


@pytest.mark.parametrize('kind', ['ADD', 'REMOVE'])
def test_completion_between_resolution_and_billing_rejects(setup, monkeypatch, kind):
    import edge.cart_session as module
    sid, pid = setup
    if kind == 'REMOVE':
        billing.add_item(sid, pid)
    rt = runtime()
    attr = '_billing_add' if kind == 'ADD' else '_billing_remove'
    original = getattr(module, attr)
    def complete_first(*args):
        billing.complete_session(sid)
        return original(*args)
    monkeypatch.setattr(module, attr, complete_first)
    acquire(rt, 75 if kind == 'ADD' else -75)
    assert dispatch_next(rt).status == 'rejected'
    assert not rt.session.last_mutation_committed
    assert len(billing.get_session_log(sid)) == (0 if kind == 'ADD' else 1)
    assert len(rt.session.cart_state.snapshot()) == (0 if kind == 'ADD' else 1)


def test_sqlite_failure_rolls_back_bill_log_total(setup):
    sid, pid = setup
    with database.get_connection() as conn:
        conn.execute("CREATE TRIGGER fail_total BEFORE UPDATE OF total_amount ON sessions "
                     "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    rt = runtime()
    acquire(rt, 75)
    assert dispatch_next(rt).status == 'rejected'
    assert billing.get_bill(sid) == []
    assert billing.get_session_log(sid) == []
    assert rt.session.cart_state.snapshot() == []
    assert billing.get_session_meta(sid)['total_amount'] == 0


def test_partial_simulation_input_does_not_block_acquisition(monkeypatch):
    import edge.runtime.weight_source as module
    source = module.StdinSimulatedWeightSource()
    monkeypatch.setattr(module.sys, 'stdin', SimpleNamespace(fileno=lambda: 42))
    monkeypatch.setattr(module.select, 'select', lambda *args: ([42], [], []))
    chunks = iter([b'7', b'5\n-75\n'])
    monkeypatch.setattr(module.os, 'read', lambda *args: next(chunks))
    assert source.poll() is None
    assert source.poll().weight_change == 75
    assert source.poll().weight_change == -75


@pytest.mark.parametrize('operation, expected', [('ADD', 20), ('REMOVE', 0)])
def test_total_reader_cannot_overwrite_concurrent_commit(setup, monkeypatch, operation, expected):
    sid, pid = setup
    billing.add_item(sid, pid)
    snapshot_read, writer_done = Event(), Event()
    original = billing.get_connection
    statements, result = [], []

    class ReadConnection:
        def __init__(self):
            self.connection = original()
            self.connection.set_trace_callback(statements.append)
        def execute(self, *args):
            cursor = self.connection.execute(*args)
            class Snapshot:
                def fetchone(self):
                    row = cursor.fetchone()
                    cursor.close()
                    snapshot_read.set()
                    assert writer_done.wait(3)
                    return row
            return Snapshot()
        def close(self):
            self.connection.close()

    from threading import current_thread
    reader = Thread(target=lambda: result.append(billing.calculate_total(sid)))
    monkeypatch.setattr(billing, 'get_connection',
                        lambda: ReadConnection() if current_thread() is reader else original())
    reader.start()
    try:
        assert snapshot_read.wait(3)
        mutation = billing.add_item if operation == 'ADD' else billing.remove_item
        assert mutation(sid, pid)
    finally:
        writer_done.set()
        reader.join(3)
    assert not reader.is_alive()
    assert result == [10.0]  # A read snapshot may age; it must never write back.
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    assert billing.calculate_total(sid) == expected
    assert billing.get_session_meta(sid)['total_amount'] == expected
    assert sum(row[4] for row in billing.get_bill(sid)) == expected
    assert len(billing.get_session_log(sid)) == 2


def test_total_read_completed_and_missing_sessions_is_non_mutating(setup, monkeypatch):
    sid, pid = setup
    billing.add_item(sid, pid)
    billing.complete_session(sid)
    original, statements = billing.get_connection, []
    def traced():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(billing, 'get_connection', traced)
    assert billing.calculate_total(sid) == 10.0
    assert billing.calculate_total(sid + 1000) == 0.0
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    with pytest.raises(billing.InactiveSessionError):
        billing.add_item(sid, pid)
    with pytest.raises(billing.InactiveSessionError):
        billing.remove_item(sid, pid)


@pytest.mark.parametrize('blocked', ['camera', 'detector', 'cleanup'])
def test_shutdown_deadline_for_blocking_dependencies(setup, blocked):
    from edge.runtime.cart_runtime import ShutdownError
    rt = runtime()
    rt.shutdown_timeout = .15
    entered, unblock = Event(), Event()
    class BlockingCamera(Camera):
        def read(self):
            if blocked == 'camera':
                entered.set()
                unblock.wait()
            else:
                rt.stopping.wait()
            return False, None
        def release(self):
            self.released = True
            if blocked == 'cleanup':
                entered.set()
                unblock.wait()
    rt.camera = BlockingCamera()
    if blocked == 'detector':
        def detect(_):
            entered.set()
            unblock.wait()
            return [Detection('Snack', .9)]
        rt.detector.detect = detect
        acquire(rt, 75)
    errors, finished = [], Event()
    def run():
        try:
            rt.run()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    supervisor = Thread(target=run)
    supervisor.start()
    try:
        if blocked != 'cleanup':
            assert entered.wait(3)
        rt.stop()
        assert finished.wait(2), 'Shutdown failed to honor its deadline'
        assert len(errors) == 1 and isinstance(errors[0], ShutdownError)
        assert rt.failed.is_set()
        assert rt.camera.released and rt.weights.closed
        assert rt._worker.daemon and rt._acquisition.daemon
    finally:
        unblock.set()
        rt.stop()
        supervisor.join(3)
        rt._worker.join(3)
        rt._acquisition.join(3)
    assert not rt._worker.is_alive() and not rt._acquisition.is_alive()
    assert billing.get_bill(setup[0]) == []  # Late inference cannot begin billing.


def test_camera_release_unblocks_clean_shutdown(setup):
    rt = runtime()
    entered, released = Event(), Event()
    class ReleasableCamera(Camera):
        def read(self):
            entered.set()
            assert released.wait(3)
            return False, None
        def release(self):
            self.released = True
            released.set()
    rt.camera = ReleasableCamera()
    errors = []
    def run():
        try:
            rt.run()
        except BaseException as error:
            errors.append(error)
    supervisor = Thread(target=run)
    supervisor.start()
    try:
        assert entered.wait(3)
        rt.stop()
        supervisor.join(3)
        assert not supervisor.is_alive()
        assert not errors
        assert rt.camera.released and rt.weights.closed
    finally:
        released.set()
        rt.stop()
        supervisor.join(3)


def test_uncertain_shutdown_retains_cart_lock_until_process_exit(setup):
    from edge.runtime import run_cart as cli
    from edge.runtime.cart_runtime import ShutdownError
    count = len(cli._quarantined_locks)
    try:
        with pytest.raises(ShutdownError):
            with cart_lock('CART-A'):
                raise ShutdownError('blocked writer')
        with pytest.raises(RuntimeError, match='already owns'):
            with cart_lock('CART-A'):
                pass
    finally:
        for handle in cli._quarantined_locks[count:]:
            handle.close()
        del cli._quarantined_locks[count:]
