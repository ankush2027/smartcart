"""
Central event orchestrator for SmartCart.

Coordinates:
    DetectionEvent (from MOG2)  +  WeightEvent (from weight sensor)
    → confirm_timed_event()
    → Confirmed ADD / REMOVE

The orchestrator does NOT implement any detection logic.
It holds at most one pending event of each type and attempts
correlation whenever a new event arrives.  Stale unmatched
events are discarded automatically.
"""

from edge.detection.confirmation import (
    confirm_timed_event,
    CORRELATION_WINDOW,
)


class EventOrchestrator:
    """
    Receives movement and weight events from their respective
    subsystems and correlates them using confirm_timed_event().

    Usage:
        orchestrator = EventOrchestrator()

        # When MOG2 detects movement:
        confirmed = orchestrator.on_movement(movement_event)

        # When weight subsystem detects a change:
        confirmed = orchestrator.on_weight(weight_event)

        # If confirmed is not None, it is a confirmed ADD/REMOVE dict.
    """

    def __init__(self, max_delay=CORRELATION_WINDOW):
        self.max_delay = max_delay

        # Pending event slots — at most one of each type.
        self._pending_movement = None
        self._pending_weight = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_movement(self, movement_event):
        """
        Receive a DetectionEvent from MOG2.

        If a pending WeightEvent exists and is within the timing
        window, returns a confirmed event dict.
        Otherwise stores the movement event as pending and returns None.
        """
        self._expire_stale_weight(movement_event.timestamp)

        if self._pending_weight is not None:
            confirmed = confirm_timed_event(
                movement_event,
                self._pending_weight,
                max_delay=self.max_delay,
            )

            if confirmed is not None:
                self._pending_weight = None
                # Do not store this movement — it has been consumed.
                return confirmed

        # No match yet — store as pending, replacing any older one.
        self._pending_movement = movement_event
        return None

    def on_weight(self, weight_event):
        """
        Receive a WeightEvent from the weight subsystem.

        If a pending DetectionEvent exists and is within the timing
        window, returns a confirmed event dict.
        Otherwise stores the weight event as pending and returns None.
        """
        self._expire_stale_movement(weight_event.timestamp)

        if self._pending_movement is not None:
            confirmed = confirm_timed_event(
                self._pending_movement,
                weight_event,
                max_delay=self.max_delay,
            )

            if confirmed is not None:
                self._pending_movement = None
                # Do not store this weight event — it has been consumed.
                return confirmed

        # No match yet — store as pending, replacing any older one.
        self._pending_weight = weight_event
        return None

    # ------------------------------------------------------------------
    # Stale-event expiry
    # ------------------------------------------------------------------

    def _expire_stale_movement(self, reference_time):
        """Discard the pending movement event if it is too old."""
        if self._pending_movement is None:
            return

        age = abs(
            (reference_time - self._pending_movement.timestamp).total_seconds()
        )

        if age > self.max_delay:
            self._pending_movement = None

    def _expire_stale_weight(self, reference_time):
        """Discard the pending weight event if it is too old."""
        if self._pending_weight is None:
            return

        age = abs(
            (reference_time - self._pending_weight.timestamp).total_seconds()
        )

        if age > self.max_delay:
            self._pending_weight = None

    # ------------------------------------------------------------------
    # Inspection helpers (useful for tests and debugging)
    # ------------------------------------------------------------------

    @property
    def pending_movement(self):
        """The currently stored pending movement event, or None."""
        return self._pending_movement

    @property
    def pending_weight(self):
        """The currently stored pending weight event, or None."""
        return self._pending_weight
