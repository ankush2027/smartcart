"""Deterministic post-movement selection and bounded runtime waiting."""
from datetime import datetime, timedelta
from threading import Event, Thread

import pytest
from edge.detection.frame_buffer import FrameBuffer
from edge.detection.frame_selector import FrameSelector, FrameSelectionError
from edge.runtime.recognition_frames import RecognitionFrames, DEFAULT_RECOGNITION_DELAY
from tests.test_cart_runtime import setup, runtime, acquire, dispatch_next
from edge.billing import billing

T = datetime(2026, 1, 1)


def confirmation(offset=0):
    ts = T + timedelta(seconds=offset)
    return {'confirmed': {'movement_timestamp': ts, 'weight_timestamp': ts,
            'event_type': 'ADD', 'weight_change': 75, 'confirmed': True}, 'frame': 'early'}


def test_later_frame_exactly_once_and_duplicate_ignored():
    selector = RecognitionFrames(clock=lambda: 0, selector=FrameSelector(scorer=lambda frame: 1 if frame == "target" else 0))
    buffer = FrameBuffer()
    buffer.add('early', T)
    assert selector.update(confirmation(), buffer) == []
    buffer.add('still early', T + timedelta(seconds=.49))
    assert selector.update(None, buffer) == []
    buffer.add('target', T + timedelta(seconds=.5))
    assert selector.update(None, buffer) == []
    buffer.add('end', T + timedelta(seconds=.8))
    ready = selector.update(None, buffer)
    assert len(ready) == 1 and ready[0]['frame'] == 'target'
    assert ready[0]['frame_timestamp'] > T
    assert selector.update(confirmation(), buffer) == []
    assert selector.update(None, buffer) == []


def test_late_correlation_selects_best_retained_window_candidate():
    buffer = FrameBuffer()
    for seconds in (0, .49, .51, .8):
        buffer.add(seconds, T + timedelta(seconds=seconds))
    selected = RecognitionFrames(clock=lambda: 0, selector=FrameSelector(scorer=lambda frame: 1 if frame == .51 else 0)).update(confirmation(), buffer)[0]
    assert selected['frame'] == .51


def test_frame_eviction_still_uses_later_frame():
    buffer = FrameBuffer(max_frames=2)
    for seconds in (0, .5, .7, .9):
        buffer.add(seconds, T + timedelta(seconds=seconds))
    result = RecognitionFrames(clock=lambda: 0, selector=FrameSelector(scorer=lambda frame: 0)).update(confirmation(), buffer)[0]
    assert result['frame'] == .7 and result['candidate_count'] == 1


def test_deadline_has_no_early_frame_fallback():
    now = [0]
    selector = RecognitionFrames(clock=lambda: now[0])
    buffer = FrameBuffer(); buffer.add('early', T)
    assert selector.update(confirmation(), buffer) == []
    now[0] = 1.5
    with pytest.raises(TimeoutError, match='not dispatched'):
        selector.check_timeout()
    buffer.add('too late', T + timedelta(seconds=2))
    with pytest.raises(TimeoutError):
        selector.update(None, buffer)
    selector.cancel()
    assert selector.update(confirmation(), buffer) == []


def test_pending_capacity_bounded():
    selector = RecognitionFrames(capacity=1, clock=lambda: 0)
    buffer = FrameBuffer(); buffer.add('early', T)
    selector.update(confirmation(), buffer)
    with pytest.raises(RuntimeError, match='queue full'):
        selector.update(confirmation(.1), buffer)


@pytest.mark.parametrize('kwargs', [{'delay': -1}, {'delay': float('nan')},
                                    {'max_wait': .5}, {'max_wait': float('inf')}])
def test_invalid_timing_rejected(kwargs):
    with pytest.raises(ValueError):
        RecognitionFrames(**kwargs)


def test_runtime_continues_acquiring_then_dispatches_once(setup):
    rt = runtime(recognition_delay=DEFAULT_RECOGNITION_DELAY)
    acquire(rt, 75)
    assert rt.jobs.empty() and not rt.detector.calls
    for offset in (.1, .2, .3, .4):
        acquire(rt, movement=False, offset=offset)
        assert rt.jobs.empty()
    assert len(rt.frame_buffer) == 5
    acquire(rt, movement=False, offset=.5)
    assert rt.jobs.empty()
    acquire(rt, movement=False, offset=.8)
    assert rt.jobs.qsize() == 1
    job = rt.jobs.queue[0]
    assert job['frame_timestamp'] == T + timedelta(seconds=.3)  # Equal-score tie: earliest.
    assert job['candidate_count'] == 4
    assert job['confirmed']['movement_timestamp'] == T
    assert job['window_start'] == T + timedelta(seconds=.3)
    assert job['window_end'] == T + timedelta(seconds=.8)
    assert dispatch_next(rt).status == 'committed'
    acquire(rt, movement=False, offset=.9)
    assert rt.jobs.empty() and len(rt.detector.calls) == 1
    assert len(billing.get_session_log(setup[0])) == 1


def test_stop_cancels_unselected_event_without_billing(setup):
    rt = runtime(recognition_delay=DEFAULT_RECOGNITION_DELAY)
    acquire(rt, 75)
    rt.stop()
    acquire(rt, movement=False, offset=.5)
    assert rt.jobs.empty() and not rt.detector.calls
    assert billing.get_bill(setup[0]) == []


def test_supervisor_deadline_when_camera_blocks_after_correlation(setup):
    rt = runtime(recognition_delay=DEFAULT_RECOGNITION_DELAY)
    now = [0]
    rt.recognition_frames.clock = lambda: now[0]
    acquire(rt, 75)
    entered, released = Event(), Event()
    class BlockedCamera:
        def read(self):
            entered.set()
            released.wait()
            return False, None
        def release(self):
            released.set()
    rt.camera = BlockedCamera()
    errors = []
    def run():
        try:
            rt.run()
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        now[0] = 2
        thread.join(2)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        assert released.is_set() and rt.weights.closed
        assert not rt._worker.is_alive() and not rt._acquisition.is_alive()
        assert not rt.detector.calls and billing.get_bill(setup[0]) == []
    finally:
        rt.stop(); released.set(); thread.join(2)


def test_no_candidates_fails_closed_without_early_fallback():
    selector = RecognitionFrames(clock=lambda: 0)
    buffer = FrameBuffer()
    buffer.add('early', T)
    selector.update(confirmation(), buffer)
    buffer.add('late', T + timedelta(seconds=1))
    with pytest.raises(FrameSelectionError, match='No frames'):
        selector.update(None, buffer)
    assert selector.update(confirmation(), buffer) == []


def test_scoring_failure_cancels_pending_without_dispatch():
    def fail(frame):
        raise ValueError('scoring failure')
    selector = RecognitionFrames(clock=lambda: 0, selector=FrameSelector(scorer=fail))
    buffer = FrameBuffer(); buffer.add('frame', T + timedelta(seconds=.8))
    with pytest.raises(ValueError, match='scoring failure'):
        selector.update(confirmation(), buffer)
    assert selector.update(confirmation(), buffer) == []


def test_scoring_does_not_hold_shutdown_lock():
    entered, release, cancelled = Event(), Event(), Event()
    def score(frame):
        entered.set()
        assert release.wait(3)
        return 1
    selector = RecognitionFrames(clock=lambda: 0, selector=FrameSelector(scorer=score))
    buffer = FrameBuffer(); buffer.add('frame', T + timedelta(seconds=.8))
    results = []
    thread = Thread(target=lambda: results.extend(selector.update(confirmation(), buffer)))
    thread.start()
    closer = Thread(target=lambda: (selector.cancel(), cancelled.set()))
    try:
        assert entered.wait(2)
        closer.start()
        assert cancelled.wait(1), 'Cancel blocked behind image scoring'
    finally:
        release.set(); thread.join(3)
        if closer.ident is not None:
            closer.join(3)
    assert not thread.is_alive() and results == []
