from dataclasses import dataclass
from datetime import datetime

@dataclass
class DetectionEvent:
    timestamp: datetime
    event_type: str
    bounding_box: tuple
    foreground_pixels: int


def create_movement_event(bounding_box, foreground_pixels):
    return DetectionEvent(
        timestamp=datetime.now(),
        event_type="MOVEMENT",
        bounding_box=bounding_box,
        foreground_pixels=foreground_pixels
    )
