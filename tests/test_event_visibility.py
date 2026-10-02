"""Event-level observability without changing correlation behavior."""
from datetime import datetime, timedelta
import logging
import pytest
from edge.orchestrator import EventOrchestrator
from edge.detection.event import DetectionEvent
from edge.weight.weight_event import WeightEvent
from tests.test_cart_runtime import setup, runtime, acquire, dispatch_next


def events(seconds):
    now = datetime(2026, 1, 1)
    return (DetectionEvent(now, 'MOVEMENT', (), 1000),
            WeightEvent(now + timedelta(seconds=seconds), 'ADD', 500, 550, 50))


@pytest.mark.parametrize('weight_first', [False, True])
def test_success_logging_preserves_inclusive_window(caplog, weight_first):
    caplog.set_level(logging.INFO)
    motion, weight = events(1)
    orchestrator = EventOrchestrator()
    if weight_first:
        assert orchestrator.on_weight(weight) is None
        result = orchestrator.on_movement(motion)
    else:
        assert orchestrator.on_movement(motion) is None
        result = orchestrator.on_weight(weight)
    assert result['confirmed'] and result['time_diff'] == 1
    assert 'Correlation pending:' in caplog.text
    assert 'Correlation succeeded: ADD dt=1.000s window=1.000s' in caplog.text


@pytest.mark.parametrize('weight_first', [False, True])
def test_expiry_logging_and_later_valid_pair(caplog, weight_first):
    caplog.set_level(logging.INFO)
    motion, weight = events(2)
    orchestrator = EventOrchestrator()
    if weight_first:
        assert orchestrator.on_weight(weight) is None
        assert orchestrator.on_movement(motion) is None
        assert orchestrator.pending_weight is None
        weight.timestamp = motion.timestamp
        result = orchestrator.on_weight(weight)
    else:
        assert orchestrator.on_movement(motion) is None
        assert orchestrator.on_weight(weight) is None
        assert orchestrator.pending_movement is None
        motion.timestamp = weight.timestamp
        result = orchestrator.on_movement(motion)
    assert result['confirmed']
    assert 'Correlation missed: expired' in caplog.text
    assert 'dt=2.000s exceeds window=1.000s' in caplog.text


def test_runtime_logs_event_boundaries_not_idle_frames(setup, caplog):
    caplog.set_level(logging.INFO)
    rt = runtime()
    for _ in range(20):
        acquire(rt, movement=False)
    assert not caplog.records
    acquire(rt, 75)
    assert 'Movement received: timestamp=' in caplog.text
    assert 'Simulated weight received: delta=+75 g direction=ADD' in caplog.text
    assert 'Dispatch job queued: ADD frame_present=True' in caplog.text
    assert dispatch_next(rt).status == 'committed'
    assert 'Recognition result:' in caplog.text
    assert 'Resolver/session result: direction=ADD resolved=True success=True' in caplog.text


def test_pending_weight_logged_even_without_motion(setup, caplog):
    caplog.set_level(logging.INFO)
    rt = runtime()
    acquire(rt, 75, movement=False)
    assert rt.jobs.empty()
    assert 'waiting for movement' in caplog.text
    assert 'Dispatch job queued' not in caplog.text
