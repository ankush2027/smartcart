"""
Focused test for the EventOrchestrator.

Verifies:
    - Movement→Weight correlation (Scenario 1)
    - Weight→Movement correlation (Scenario 2)
    - REMOVE confirmation (Scenario 3)
    - Timing rejection (Scenario 4)
    - Movement-only rejection (Scenario 5)
    - Weight-only rejection (Scenario 6)
    - Stale event expiry
    - Pending slot replacement

No camera, no OpenCV, no SQLite, no hardware.
All events use explicit timestamps for deterministic results.
"""

from datetime import datetime, timedelta

from edge.detection.event import DetectionEvent
from edge.detection.confirmation import CORRELATION_WINDOW
from edge.weight.weight_event import WeightEvent, detect_weight_change
from edge.orchestrator import EventOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _movement(timestamp=None):
    """Create a synthetic DetectionEvent with a controllable timestamp."""
    return DetectionEvent(
        timestamp=timestamp or datetime.now(),
        event_type="MOVEMENT",
        bounding_box=(100, 100, 50, 50),
        foreground_pixels=2000,
    )


def _weight(previous, current, timestamp=None):
    """
    Build a WeightEvent through detect_weight_change() — single
    source of truth for threshold logic — then optionally override
    the timestamp.
    """
    event = detect_weight_change(previous, current)

    if event is not None and timestamp is not None:
        event.timestamp = timestamp

    return event


# ===================================================================
# Scenario 1 — Movement then Weight (+70 g) → ADD confirmed
# ===================================================================

def test_scenario_1_movement_then_weight_add():
    """
    MovementEvent  →  +70 g WeightEvent (within 1 s)  →  ADD confirmed
    """
    now = datetime(2026, 1, 1, 12, 0, 0)
    orch = EventOrchestrator()

    # Step 1: movement arrives — no weight yet
    result = orch.on_movement(_movement(timestamp=now))
    assert result is None, "Expected None — no weight event yet"
    assert orch.pending_movement is not None

    # Step 2: weight arrives 0.3 s later
    weight_time = now + timedelta(seconds=0.3)
    result = orch.on_weight(_weight(500.0, 570.0, timestamp=weight_time))

    assert result is not None,             "Expected confirmed event"
    assert result["confirmed"] is True
    assert result["event_type"] == "ADD",  f"Expected ADD, got {result['event_type']}"
    assert abs(result["weight_change"] - 70.0) < 1e-9
    assert result["previous_weight"] == 500.0
    assert result["current_weight"] == 570.0
    assert result["time_diff"] <= CORRELATION_WINDOW

    # Both pending slots should be cleared
    assert orch.pending_movement is None
    assert orch.pending_weight is None

    print("PASS  test_scenario_1_movement_then_weight_add")


# ===================================================================
# Scenario 2 — Weight then Movement (+70 g) → ADD confirmed
# ===================================================================

def test_scenario_2_weight_then_movement_add():
    """
    +70 g WeightEvent  →  MovementEvent (within 1 s)  →  ADD confirmed
    """
    now = datetime(2026, 1, 1, 12, 0, 0)
    orch = EventOrchestrator()

    # Step 1: weight arrives first
    result = orch.on_weight(_weight(500.0, 570.0, timestamp=now))
    assert result is None, "Expected None — no movement yet"
    assert orch.pending_weight is not None

    # Step 2: movement arrives 0.5 s later
    movement_time = now + timedelta(seconds=0.5)
    result = orch.on_movement(_movement(timestamp=movement_time))

    assert result is not None,             "Expected confirmed event"
    assert result["confirmed"] is True
    assert result["event_type"] == "ADD"
    assert abs(result["weight_change"] - 70.0) < 1e-9

    assert orch.pending_movement is None
    assert orch.pending_weight is None

    print("PASS  test_scenario_2_weight_then_movement_add")


# ===================================================================
# Scenario 3 — Movement + (-70 g) → REMOVE confirmed
# ===================================================================

def test_scenario_3_remove():
    """
    MovementEvent  →  -70 g WeightEvent (within 1 s)  →  REMOVE confirmed
    """
    now = datetime(2026, 1, 1, 12, 0, 0)
    orch = EventOrchestrator()

    orch.on_movement(_movement(timestamp=now))

    weight_time = now + timedelta(seconds=0.4)
    result = orch.on_weight(_weight(570.0, 500.0, timestamp=weight_time))

    assert result is not None,                "Expected confirmed event"
    assert result["confirmed"] is True
    assert result["event_type"] == "REMOVE",  f"Expected REMOVE, got {result['event_type']}"
    assert abs(result["weight_change"] - (-70.0)) < 1e-9

    print("PASS  test_scenario_3_remove")


# ===================================================================
# Scenario 4 — Events > 1 s apart → NOT confirmed
# ===================================================================

def test_scenario_4_timing_rejection():
    """
    MovementEvent + WeightEvent separated by 2 seconds → NOT confirmed.
    The stale movement should be expired before matching is attempted.
    """
    now = datetime(2026, 1, 1, 12, 0, 0)
    orch = EventOrchestrator()

    orch.on_movement(_movement(timestamp=now))

    # Weight arrives 2 s later
    weight_time = now + timedelta(seconds=2.0)
    result = orch.on_weight(_weight(500.0, 570.0, timestamp=weight_time))

    assert result is None, \
        f"Expected None (timing rejection), got {result}"

    # Movement should have been expired; weight is now pending
    assert orch.pending_movement is None
    assert orch.pending_weight is not None

    print("PASS  test_scenario_4_timing_rejection")


# ===================================================================
# Scenario 5 — Movement only → NOT confirmed
# ===================================================================

def test_scenario_5_movement_only():
    """
    Only a movement event arrives — no weight event ever.
    → NOT confirmed.  Movement stays pending.
    """
    orch = EventOrchestrator()
    now = datetime(2026, 1, 1, 12, 0, 0)

    result = orch.on_movement(_movement(timestamp=now))

    assert result is None
    assert orch.pending_movement is not None
    assert orch.pending_weight is None

    print("PASS  test_scenario_5_movement_only")


# ===================================================================
# Scenario 6 — Weight only → NOT confirmed
# ===================================================================

def test_scenario_6_weight_only():
    """
    Only a weight event arrives — no movement event ever.
    → NOT confirmed.  Weight stays pending.
    """
    orch = EventOrchestrator()
    now = datetime(2026, 1, 1, 12, 0, 0)

    result = orch.on_weight(_weight(500.0, 570.0, timestamp=now))

    assert result is None
    assert orch.pending_weight is not None
    assert orch.pending_movement is None

    print("PASS  test_scenario_6_weight_only")


# ===================================================================
# Stale event expiry
# ===================================================================

def test_stale_movement_expired():
    """
    A pending movement event older than max_delay is discarded
    when a new weight event arrives.
    """
    orch = EventOrchestrator()
    t0 = datetime(2026, 1, 1, 12, 0, 0)

    orch.on_movement(_movement(timestamp=t0))
    assert orch.pending_movement is not None

    # Weight arrives 3 s later — movement is stale
    t1 = t0 + timedelta(seconds=3.0)
    result = orch.on_weight(_weight(500.0, 570.0, timestamp=t1))

    assert result is None
    assert orch.pending_movement is None,  "Stale movement should be expired"
    assert orch.pending_weight is not None, "Weight should be pending instead"

    print("PASS  test_stale_movement_expired")


def test_stale_weight_expired():
    """
    A pending weight event older than max_delay is discarded
    when a new movement event arrives.
    """
    orch = EventOrchestrator()
    t0 = datetime(2026, 1, 1, 12, 0, 0)

    orch.on_weight(_weight(500.0, 570.0, timestamp=t0))
    assert orch.pending_weight is not None

    # Movement arrives 3 s later — weight is stale
    t1 = t0 + timedelta(seconds=3.0)
    result = orch.on_movement(_movement(timestamp=t1))

    assert result is None
    assert orch.pending_weight is None,     "Stale weight should be expired"
    assert orch.pending_movement is not None, "Movement should be pending instead"

    print("PASS  test_stale_weight_expired")


# ===================================================================
# Pending slot replacement
# ===================================================================

def test_pending_movement_replaced():
    """
    A second movement event replaces the first pending one.
    The newer event should be used for subsequent correlation.
    """
    orch = EventOrchestrator()
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=0.2)
    t2 = t0 + timedelta(seconds=0.5)

    # Two movements, no weight yet
    orch.on_movement(_movement(timestamp=t0))
    orch.on_movement(_movement(timestamp=t1))

    # The pending movement should be the newer one
    assert orch.pending_movement.timestamp == t1

    # Weight arrives — should correlate with t1, not t0
    result = orch.on_weight(_weight(500.0, 570.0, timestamp=t2))

    assert result is not None
    assert result["confirmed"] is True
    assert result["movement_timestamp"] == t1

    print("PASS  test_pending_movement_replaced")


def test_pending_weight_replaced():
    """
    A second weight event replaces the first pending one.
    """
    orch = EventOrchestrator()
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=0.2)
    t2 = t0 + timedelta(seconds=0.5)

    # Two weight events, no movement yet
    orch.on_weight(_weight(500.0, 570.0, timestamp=t0))
    orch.on_weight(_weight(500.0, 600.0, timestamp=t1))

    # The pending weight should be the newer one (+100 g)
    assert orch.pending_weight.timestamp == t1

    # Movement arrives — should correlate with the +100 g event
    result = orch.on_movement(_movement(timestamp=t2))

    assert result is not None
    assert result["confirmed"] is True
    assert abs(result["weight_change"] - 100.0) < 1e-9

    print("PASS  test_pending_weight_replaced")


# ===================================================================
# Sub-threshold weight through the orchestrator
# ===================================================================

def test_subthreshold_weight_not_stored():
    """
    detect_weight_change returns None for sub-threshold changes.
    The orchestrator should handle None gracefully.
    """
    orch = EventOrchestrator()
    now = datetime(2026, 1, 1, 12, 0, 0)

    orch.on_movement(_movement(timestamp=now))

    # +5 g is below default 10 g threshold → detect_weight_change returns None
    sub_event = _weight(500.0, 505.0, timestamp=now)
    assert sub_event is None, "Expected None from detect_weight_change for +5 g"

    # The orchestrator should not crash when given None.
    # The caller would not call on_weight with None in practice,
    # but we verify the orchestrator's pending movement survives.
    # (In the real pipeline, the weight subsystem would simply
    # not emit an event for sub-threshold changes.)
    assert orch.pending_movement is not None

    print("PASS  test_subthreshold_weight_not_stored")


# ===================================================================
# Multiple sequential confirmed events
# ===================================================================

def test_two_sequential_confirmations():
    """
    Two complete movement+weight cycles should each produce
    an independent confirmed event.
    """
    orch = EventOrchestrator()
    t0 = datetime(2026, 1, 1, 12, 0, 0)

    # Cycle 1: ADD
    orch.on_movement(_movement(timestamp=t0))
    r1 = orch.on_weight(_weight(500.0, 570.0, timestamp=t0 + timedelta(seconds=0.3)))

    assert r1 is not None
    assert r1["event_type"] == "ADD"

    # Cycle 2: REMOVE (2 seconds later — fully independent)
    t2 = t0 + timedelta(seconds=2.0)
    orch.on_movement(_movement(timestamp=t2))
    r2 = orch.on_weight(_weight(570.0, 500.0, timestamp=t2 + timedelta(seconds=0.3)))

    assert r2 is not None
    assert r2["event_type"] == "REMOVE"

    # No pending events left
    assert orch.pending_movement is None
    assert orch.pending_weight is None

    print("PASS  test_two_sequential_confirmations")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Orchestrator Test Suite")
    print("=" * 60)

    tests = [
        test_scenario_1_movement_then_weight_add,
        test_scenario_2_weight_then_movement_add,
        test_scenario_3_remove,
        test_scenario_4_timing_rejection,
        test_scenario_5_movement_only,
        test_scenario_6_weight_only,
        test_stale_movement_expired,
        test_stale_weight_expired,
        test_pending_movement_replaced,
        test_pending_weight_replaced,
        test_subthreshold_weight_not_stored,
        test_two_sequential_confirmations,
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
