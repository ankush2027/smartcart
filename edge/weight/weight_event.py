from dataclasses import dataclass
from datetime import datetime


@dataclass
class WeightEvent:
    timestamp: datetime
    event_type: str
    previous_weight: float
    current_weight: float
    weight_change: float


def detect_weight_change(previous_weight, current_weight, threshold=10.0):
    """
    Detect whether weight increased or decreased.

    threshold is the minimum weight change in grams
    required to create an event.
    """

    change = current_weight - previous_weight

    if change >= threshold:
        return WeightEvent(
            timestamp=datetime.now(),
            event_type="ADD",
            previous_weight=previous_weight,
            current_weight=current_weight,
            weight_change=change
        )

    if change <= -threshold:
        return WeightEvent(
            timestamp=datetime.now(),
            event_type="REMOVE",
            previous_weight=previous_weight,
            current_weight=current_weight,
            weight_change=change
        )

    return None


def main():
    previous_weight = 500.0

    print("Previous weight:", previous_weight, "g")

    # Simulate product being added
    current_weight = 575.0

    event = detect_weight_change(
        previous_weight,
        current_weight
    )

    if event:
        print("\nWeight event detected:")
        print(event)
    else:
        print("\nNo significant weight change")


if __name__ == "__main__":
    main()