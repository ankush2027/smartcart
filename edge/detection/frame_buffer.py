"""
Rolling frame buffer for recent camera frames.

Stores a bounded number of recent (timestamp, frame) pairs
so that when a confirmed event occurs, the system can retrieve
the frame closest to that event's timestamp — for example to
pass to a future object-detection stage.

Responsibilities:
    - Store recent frames with timestamps
    - Discard oldest frames automatically when full
    - Retrieve the frame closest to a given timestamp

Non-responsibilities:
    - Does NOT perform movement detection
    - Does NOT perform object detection / YOLO
    - Does NOT perform billing or event confirmation
"""

from collections import deque
from datetime import datetime


# Default maximum number of frames to retain.
# At 30 fps this covers ~1 second of history, which aligns
# with the 1-second CORRELATION_WINDOW used by the orchestrator.
DEFAULT_MAX_FRAMES = 30

# When searching for the closest frame, ignore frames that are
# farther than this many seconds from the requested timestamp.
DEFAULT_MAX_AGE = 2.0


class FrameBuffer:
    """
    Bounded rolling buffer of (timestamp, frame) pairs.

    Usage:
        buffer = FrameBuffer(max_frames=30)

        # In the camera loop:
        buffer.add(frame)                        # auto-timestamps with now()
        buffer.add(frame, timestamp=custom_ts)   # explicit timestamp

        # After a confirmed event:
        entry = buffer.get_closest_frame(event_timestamp)
        if entry is not None:
            ts, frame = entry
    """

    def __init__(self, max_frames=DEFAULT_MAX_FRAMES, max_age=DEFAULT_MAX_AGE):
        self._max_age = max_age
        self._buffer = deque(maxlen=max_frames)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add(self, frame, timestamp=None):
        """
        Store a frame with its capture timestamp.

        If timestamp is not provided, datetime.now() is used.
        When the buffer is full the oldest entry is automatically
        discarded by the deque.
        """
        if timestamp is None:
            timestamp = datetime.now()

        self._buffer.append((timestamp, frame))

    def get_recent_frames(self, count=None):
        """
        Return the most recent frames as a list of (timestamp, frame).

        If count is None, returns all buffered frames.
        Otherwise returns at most `count` most-recent entries.
        """
        if count is None:
            return list(self._buffer)

        return list(self._buffer)[-count:]

    def get_closest_frame(self, target_timestamp):
        """
        Return the (timestamp, frame) entry whose timestamp is
        closest to target_timestamp.

        Returns None if:
            - the buffer is empty, or
            - the closest frame is farther than max_age seconds
              from target_timestamp.
        """
        if len(self._buffer) == 0:
            return None

        best_entry = None
        best_diff = None

        for entry in self._buffer:
            ts = entry[0]
            diff = abs((ts - target_timestamp).total_seconds())

            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_entry = entry

        if best_diff > self._max_age:
            return None

        return best_entry

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def __len__(self):
        """Number of frames currently in the buffer."""
        return len(self._buffer)

    @property
    def max_frames(self):
        """Maximum capacity of the buffer."""
        return self._buffer.maxlen

    @property
    def max_age(self):
        """Maximum allowed age (seconds) for closest-frame lookup."""
        return self._max_age

    def clear(self):
        """Remove all frames from the buffer."""
        self._buffer.clear()
