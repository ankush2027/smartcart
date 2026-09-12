"""
Integration test: MOG2 movement events + weight events → confirmed cart events.

Tests the full pipeline:
    DetectionEvent (from MOG2)  ─┐
                                 ├→  confirm_timed_event()  →  Confirmed ADD / REMOVE
    WeightEvent (from sensor)  ─┘

No camera, no OpenCV, no SQLite.
All timestamps are constructed explicitly to test timing logic.
"""

from datetime import datetime, timedelta

from edge.detection.event import DetectionEvent, create_movement_event
from edge.detection.confirmation import (
    confirm_timed_event,
    CORRELATION_WINDOW,
)
from edge.weight.weight import WeightSensor
from edge.weight.weight_event import WeightEvent, detect_weight_change


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_movement_event(timestamp=None):
    """
    Create a DetectionEvent with a controllable timestamp.
    Bounding box and foreground pixels are synthetic but structurally valid.
    """
    return DetectionEvent(
        timestamp=timestamp or datetime.now(),
        event_type="MOVEMENT",
        bounding_box=(100, 100, 50, 50),
        foreground_pixels=2000
    )


def _make_weight_event(previous, current, timestamp=None):
    """
    Build a WeightEvent through detect_weight_change() — the single
    source of truth for threshold logic — then optionally override the
    timestamp for timing tests.
    """
    event = detect_weight_change(previous, current)

    if event is not None and timestamp is not None:
        # Replace timestamp for timing-correlation testing.
        # dataclasses allow direct attribute assignment.
        event.timestamp = timestamp

    return event


# ===================================================================
# TASK 3C — Confirmed ADD
# ===================================================================

def test_confirmed_add():
    """
    movement detected  +  positive weight change  +  valid timing
    → confirmed ADD

    previous weight = 500 g
    current weight  = 570 g
    """
    now = datetime.now()

    movement = _make_movement_event(timestamp=now)
    weight   = _make_weight_event(500.0, 570.0, timestamp=now)

    result = confirm_timed_event(movement, weight)

    assert result is not None,                "Expected confirmed event, got None"
    assert result["confirmed"] is True,       "Expected confirmed=True"
    assert result["event_type"] == "ADD",     f"Expected ADD, got {result['event_type']}"
    assert abs(result["weight_change"] - 70.0) < 1e-9, \
        f"Expected weight_change≈70, got {result['weight_change']}"
    assert result["previous_weight"] == 500.0
    assert result["current_weight"]  == 570.0
    assert result["time_diff"] <= CORRELATION_WINDOW

    print("PASS  test_confirmed_add")
    print("      result:", result)


# ===================================================================
# TASK 3D — Confirmed REMOVE
# ===================================================================

def test_confirmed_remove():
    """
    movement detected  +  negative weight change  +  valid timing
    → confirmed REMOVE

    previous weight = 570 g
    current weight  = 500 g
    """
    now = datetime.now()

    movement = _make_movement_event(timestamp=now)
    weight   = _make_weight_event(570.0, 500.0, timestamp=now)

    result = confirm_timed_event(movement, weight)

    assert result is not None,                   "Expected confirmed event, got None"
    assert result["confirmed"] is True,          "Expected confirmed=True"
    assert result["event_type"] == "REMOVE",     f"Expected REMOVE, got {result['event_type']}"
    assert abs(result["weight_change"] - (-70.0)) < 1e-9, \
        f"Expected weight_change≈-70, got {result['weight_change']}"
    assert result["previous_weight"] == 570.0
    assert result["current_weight"]  == 500.0

    print("PASS  test_confirmed_remove")
    print("      result:", result)


# ===================================================================
# TASK 3E — False-event tests
# ===================================================================

def test_movement_without_weight_change():
    """
    Movement detected, but no significant weight change.
    → NOT confirmed
    """
    now = datetime.now()

    movement = _make_movement_event(timestamp=now)
    weight   = _make_weight_event(500.0, 505.0, timestamp=now)  # +5 g < 10 g threshold

    # detect_weight_change returns None for sub-threshold → weight is None
    assert weight is None, \
        f"Expected None from detect_weight_change for +5 g, got {weight}"

    result = confirm_timed_event(movement, weight)

    assert result is None, \
        f"Expected None (movement without weight change), got {result}"

    print("PASS  test_movement_without_weight_change")


def test_weight_change_without_movement():
    """
    Significant weight change, but NO movement detected.
    → NOT confirmed
    """
    now = datetime.now()

    movement = None  # no movement event
    weight   = _make_weight_event(500.0, 570.0, timestamp=now)

    result = confirm_timed_event(movement, weight)

    assert result is None, \
        f"Expected None (weight without movement), got {result}"

    print("PASS  test_weight_change_without_movement")


def test_weight_change_outside_timing_window():
    """
    Both movement and weight change exist, but they are separated
    by more than CORRELATION_WINDOW (1.0 s).
    → NOT confirmed
    """
    movement_time = datetime(2026, 1, 1, 12, 0, 0)
    weight_time   = movement_time + timedelta(seconds=2.0)  # 2 s apart

    movement = _make_movement_event(timestamp=movement_time)
    weight   = _make_weight_event(500.0, 570.0, timestamp=weight_time)

    result = confirm_timed_event(movement, weight)

    assert result is None, \
        f"Expected None (outside timing window), got {result}"

    print("PASS  test_weight_change_outside_timing_window")


def test_weight_change_below_threshold():
    """
    Movement detected + weight change below threshold.
    detect_weight_change() returns None → NOT confirmed.

    This is distinct from test_movement_without_weight_change only
    in that we verify it through the full sensor flow.
    """
    sensor = WeightSensor()
    sensor.current_weight = 500.0

    previous = sensor.read_weight()
    sensor.simulate_change(+3)              # 3 g — well below threshold
    current = sensor.read_weight()

    weight = detect_weight_change(previous, current)

    assert weight is None, \
        f"Expected None from detect_weight_change for +3 g, got {weight}"

    movement = _make_movement_event(timestamp=datetime.now())
    result   = confirm_timed_event(movement, weight)

    assert result is None, \
        f"Expected None (sub-threshold weight), got {result}"

    print("PASS  test_weight_change_below_threshold")


# ===================================================================
# Additional edge-case: events within window (near boundary)
# ===================================================================

def test_events_just_within_window():
    """
    Events 0.9 s apart — inside the 1.0 s window.
    → confirmed
    """
    movement_time = datetime(2026, 1, 1, 12, 0, 0)
    weight_time   = movement_time + timedelta(seconds=0.9)

    movement = _make_movement_event(timestamp=movement_time)
    weight   = _make_weight_event(500.0, 570.0, timestamp=weight_time)

    result = confirm_timed_event(movement, weight)

    assert result is not None,             "Expected confirmed at 0.9 s delta"
    assert result["confirmed"] is True
    assert result["time_diff"] <= CORRELATION_WINDOW

    print("PASS  test_events_just_within_window")


def test_events_just_outside_window():
    """
    Events 1.1 s apart — outside the 1.0 s window.
    → NOT confirmed
    """
    movement_time = datetime(2026, 1, 1, 12, 0, 0)
    weight_time   = movement_time + timedelta(seconds=1.1)

    movement = _make_movement_event(timestamp=movement_time)
    weight   = _make_weight_event(500.0, 570.0, timestamp=weight_time)

    result = confirm_timed_event(movement, weight)

    assert result is None, \
        f"Expected None at 1.1 s delta, got {result}"

    print("PASS  test_events_just_outside_window")


def test_weight_before_movement():
    """
    Weight event occurs 0.5 s BEFORE the movement event.
    The correlation window is symmetric (abs), so this should confirm.
    """
    movement_time = datetime(2026, 1, 1, 12, 0, 1)
    weight_time   = datetime(2026, 1, 1, 12, 0, 0, 500000)  # 0.5 s earlier

    movement = _make_movement_event(timestamp=movement_time)
    weight   = _make_weight_event(500.0, 570.0, timestamp=weight_time)

    result = confirm_timed_event(movement, weight)

    assert result is not None,           "Expected confirmed (weight before movement)"
    assert result["confirmed"] is True
    assert result["event_type"] == "ADD"

    print("PASS  test_weight_before_movement")


def test_create_movement_event_produces_detection_event():
    """
    Verify that the existing create_movement_event() factory produces
    a DetectionEvent with the correct structure for confirm_timed_event().
    """
    event = create_movement_event(
        bounding_box=(10, 20, 30, 40),
        foreground_pixels=1500
    )

    assert isinstance(event, DetectionEvent)
    assert event.event_type == "MOVEMENT"
    assert event.bounding_box == (10, 20, 30, 40)
    assert event.foreground_pixels == 1500
    assert isinstance(event.timestamp, datetime)

    # Should work with confirm_timed_event when paired with a weight event
    weight = _make_weight_event(500.0, 570.0, timestamp=event.timestamp)
    result = confirm_timed_event(event, weight)

    assert result is not None
    assert result["confirmed"] is True

    print("PASS  test_create_movement_event_produces_detection_event")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Integration Test Suite")
    print("  MOG2 Movement + Weight Events → Confirmed Cart Events")
    print("=" * 60)

    tests = [
        # Task 3C
        test_confirmed_add,
        # Task 3D
        test_confirmed_remove,
        # Task 3E — false events
        test_movement_without_weight_change,
        test_weight_change_without_movement,
        test_weight_change_outside_timing_window,
        test_weight_change_below_threshold,
        # Timing edge cases
        test_events_just_within_window,
        test_events_just_outside_window,
        test_weight_before_movement,
        # Structural compatibility
        test_create_movement_event_produces_detection_event,
    ]

    failures = []

    for test_fn in tests:
        try:
            test_fn()
        except AssertionError as exc:
            failures.append((test_fn.__name__, exc))
            print(f"FAIL  {test_fn.__name__}: {exc}")
        except Exception as exc:
            failures.append((test_fn.__name__, exc))
            print(f"ERROR {test_fn.__name__}: {exc}")

    print("=" * 60)

    if failures:
        print(f"FAILED: {len(failures)}/{len(tests)} tests")
        raise SystemExit(1)
    else:
        print(f"ALL PASSED: {len(tests)}/{len(tests)} tests")


if __name__ == "__main__":
    main()
