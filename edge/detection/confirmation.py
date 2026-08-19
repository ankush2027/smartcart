from datetime import datetime

from edge.weight.weight_event import detect_weight_change


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