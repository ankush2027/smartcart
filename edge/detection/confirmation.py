from datetime import datetime

from edge.weight.weight_event import detect_weight_change


# Default correlation window in seconds.
# Movement and weight change must occur within this window
# to be considered the same cart event.
CORRELATION_WINDOW = 1.0


def confirm_event(movement_detected, weight_event):
    """
    Confirm an event only when both movement
    and weight change are detected.
    """

    if not movement_detected:
        return None

    if weight_event is None:
        return None

    return {
        "timestamp": datetime.now(),
        "event_type": weight_event.event_type,
        "weight_change": weight_event.weight_change,
        "previous_weight": weight_event.previous_weight,
        "current_weight": weight_event.current_weight,
        "confirmed": True
    }


def confirm_timed_event(movement_event, weight_event,
                        max_delay=CORRELATION_WINDOW):
    """
    Confirm an event when both a movement DetectionEvent and a
    WeightEvent are present and occurred within max_delay seconds.

    movement_event: DetectionEvent from MOG2 (or None)
    weight_event:   WeightEvent from weight subsystem (or None)
    max_delay:      maximum allowed seconds between the two timestamps

    Returns a confirmed-event dict or None.
    """

    if movement_event is None:
        return None

    if weight_event is None:
        return None

    time_diff = abs(
        (weight_event.timestamp - movement_event.timestamp).total_seconds()
    )

    if time_diff > max_delay:
        return None

    return {
        "timestamp": datetime.now(),
        "event_type": weight_event.event_type,
        "weight_change": weight_event.weight_change,
        "previous_weight": weight_event.previous_weight,
        "current_weight": weight_event.current_weight,
        "movement_timestamp": movement_event.timestamp,
        "weight_timestamp": weight_event.timestamp,
        "time_diff": time_diff,
        "confirmed": True
    }


def main():
    previous_weight = 500.0
    current_weight = 575.0

    movement_detected = True

    weight_event = detect_weight_change(
        previous_weight,
        current_weight
    )

    event = confirm_event(
        movement_detected,
        weight_event
    )

    if event:
        print("CONFIRMED EVENT")
        print(event)
    else:
        print("No confirmed event")


if __name__ == "__main__":
    main()