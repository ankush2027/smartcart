"""
Focused test for the weight-event pipeline.

Pipeline under test:
    WeightSensor  →  detect_weight_change()  →  WeightEvent  →  confirm_event()

No SQLite database is touched.  No production state is mutated.
"""

from edge.weight.weight import WeightSensor
from edge.weight.weight_event import WeightEvent, detect_weight_change
from edge.detection.confirmation import confirm_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_then_change(sensor, delta):
    """
    Snapshot the sensor's current weight as `previous_weight`, apply `delta`,
    then return (previous_weight, new_current_weight).

    This is the canonical two-step pattern for the weight pipeline:
        1. Read weight BEFORE the physical event.
        2. Apply the simulated change.
        3. Read weight AFTER the physical event.
        4. Pass both values to detect_weight_change().
    """
    previous = sensor.read_weight()
    sensor.simulate_change(delta)
    current = sensor.read_weight()
    return previous, current


# ---------------------------------------------------------------------------
# Case 1 — ADD event
# ---------------------------------------------------------------------------

def test_add_event():
    """
    Initial weight = 500 g
    Simulate +70 g  →  current = 570 g
    Expected: WeightEvent(event_type="ADD", weight_change ≈ +70)
    """
    sensor = WeightSensor()
    sensor.current_weight = 500.0          # set explicit start state

    previous, current = _read_then_change(sensor, +70)

    assert previous == 500.0, f"Expected previous=500.0, got {previous}"
    assert current == 570.0,  f"Expected current=570.0,  got {current}"

    event = detect_weight_change(previous, current)

    assert event is not None,               "Expected a WeightEvent, got None"
    assert isinstance(event, WeightEvent),  f"Expected WeightEvent, got {type(event)}"
    assert event.event_type == "ADD",       f"Expected ADD, got {event.event_type}"
    assert abs(event.weight_change - 70.0) < 1e-9, \
        f"Expected weight_change≈70, got {event.weight_change}"

    print("PASS  test_add_event")
    print("      event:", event)


# ---------------------------------------------------------------------------
# Case 2 — REMOVE event
# ---------------------------------------------------------------------------

def test_remove_event():
    """
    Start from 570 g (end-state of Case 1).
    Simulate -70 g  →  current = 500 g
    Expected: WeightEvent(event_type="REMOVE", weight_change ≈ -70)
    """
    sensor = WeightSensor()
    sensor.current_weight = 570.0          # start from Case-1 end state

    previous, current = _read_then_change(sensor, -70)

    assert previous == 570.0, f"Expected previous=570.0, got {previous}"
    assert current == 500.0,  f"Expected current=500.0,  got {current}"

    event = detect_weight_change(previous, current)

    assert event is not None,                  "Expected a WeightEvent, got None"
    assert isinstance(event, WeightEvent),     f"Expected WeightEvent, got {type(event)}"
    assert event.event_type == "REMOVE",       f"Expected REMOVE, got {event.event_type}"
    assert abs(event.weight_change - (-70.0)) < 1e-9, \
        f"Expected weight_change≈-70, got {event.weight_change}"

    print("PASS  test_remove_event")
    print("      event:", event)


# ---------------------------------------------------------------------------
# Case 3 — below-threshold change → None
# ---------------------------------------------------------------------------

def test_below_threshold_returns_none():
    """
    Simulate a change smaller than the default threshold (10 g).
    Expected: detect_weight_change() returns None.
    """
    sensor = WeightSensor()
    sensor.current_weight = 500.0

    # Use a change of 5 g — well below the 10 g threshold
    previous, current = _read_then_change(sensor, +5)

    event = detect_weight_change(previous, current)

    assert event is None, \
        f"Expected None for sub-threshold change, got {event}"

    print("PASS  test_below_threshold_returns_none")


# ---------------------------------------------------------------------------
# Case 4 — WeightEvent flows correctly through confirm_event()
# ---------------------------------------------------------------------------

def test_confirmation_consumes_weight_event():
    """
    Verify that confirmation.confirm_event() can consume the WeightEvent
    produced by detect_weight_change() without any structural mismatch.
    """
    sensor = WeightSensor()
    sensor.current_weight = 500.0

    previous, current = _read_then_change(sensor, +70)
    weight_event = detect_weight_change(previous, current)

    # Simulate movement_detected = True (MOG2 side — not tested here)
    confirmed = confirm_event(movement_detected=True, weight_event=weight_event)

    assert confirmed is not None,                       "Expected confirmed event dict, got None"
    assert confirmed["confirmed"] is True,              "Expected confirmed=True"
    assert confirmed["event_type"] == "ADD",            f"Expected ADD, got {confirmed['event_type']}"
    assert abs(confirmed["weight_change"] - 70.0) < 1e-9, \
        f"Expected weight_change≈70, got {confirmed['weight_change']}"
    assert confirmed["previous_weight"] == 500.0,       f"Previous mismatch: {confirmed['previous_weight']}"
    assert confirmed["current_weight"] == 570.0,        f"Current mismatch: {confirmed['current_weight']}"

    print("PASS  test_confirmation_consumes_weight_event")
    print("      confirmed:", confirmed)


# ---------------------------------------------------------------------------
# Case 5 — movement absent suppresses event (confirm_event guard)
# ---------------------------------------------------------------------------

def test_confirmation_requires_movement():
    """
    confirm_event() must return None when movement_detected is False,
    even if a valid WeightEvent exists.
    """
    sensor = WeightSensor()
    sensor.current_weight = 500.0

    previous, current = _read_then_change(sensor, +70)
    weight_event = detect_weight_change(previous, current)

    result = confirm_event(movement_detected=False, weight_event=weight_event)

    assert result is None, \
        f"Expected None when no movement, got {result}"

    print("PASS  test_confirmation_requires_movement")


# ---------------------------------------------------------------------------
# Case 6 — exact threshold boundary (10 g triggers, 9.99 g does not)
# ---------------------------------------------------------------------------

def test_threshold_boundary():
    """
    Verify the threshold boundary is handled correctly by detect_weight_change().
    change == threshold (10 g) → ADD
    change < threshold (9.99 g) → None
    """
    # Exactly at threshold → should trigger
    event_at = detect_weight_change(0.0, 10.0)
    assert event_at is not None,             "Expected ADD at exactly 10 g"
    assert event_at.event_type == "ADD",     f"Expected ADD, got {event_at.event_type}"

    # Just below threshold → should not trigger
    event_below = detect_weight_change(0.0, 9.99)
    assert event_below is None,              "Expected None for 9.99 g change"

    print("PASS  test_threshold_boundary")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Weight Pipeline Test Suite")
    print("=" * 60)

    tests = [
        test_add_event,
        test_remove_event,
        test_below_threshold_returns_none,
        test_confirmation_consumes_weight_event,
        test_confirmation_requires_movement,
        test_threshold_boundary,
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
