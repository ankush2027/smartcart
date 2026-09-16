"""
MotionPipeline — integration layer between FrameBuffer and EventOrchestrator.

Responsibilities:
    - Accept a pre-captured camera frame (no camera ownership)
    - Store the frame in the FrameBuffer with the supplied timestamp
    - Forward a DetectionEvent (if supplied) to EventOrchestrator.on_movement()
    - Forward a WeightEvent   (if supplied) to EventOrchestrator.on_weight()
    - When the orchestrator produces a confirmed event, retrieve the frame
      closest to confirmed["movement_timestamp"] from the FrameBuffer
    - Return a result dict  {confirmed: ..., frame: ...}  or None

Non-responsibilities:
    - Does NOT own or open a camera
    - Does NOT run MOG2 background subtraction
    - Does NOT load or run a YOLO model
    - Does NOT touch CartSession, SQLite, or billing
    - Does NOT duplicate any detection algorithm
"""

from datetime import datetime


class MotionPipeline:
    """
    Thin integration layer that wires FrameBuffer ↔ EventOrchestrator.

    Parameters
    ----------
    frame_buffer : FrameBuffer
        Rolling buffer of recent camera frames.  Injected by the caller.
    orchestrator : EventOrchestrator
        Event correlator.  Injected by the caller.

    Usage
    -----
        pipeline = MotionPipeline(frame_buffer, orchestrator)

        # Inside the camera loop — caller provides already-decoded frame:
        result = pipeline.process_frame(
            frame,
            movement_event=detection_event,   # DetectionEvent or None
            weight_event=weight_event,         # WeightEvent or None
            timestamp=frame_timestamp,         # datetime or None
        )

        if result is not None:
            confirmed = result["confirmed"]
            matched_frame = result["frame"]   # may be None if buffer had no match
    """

    def __init__(self, frame_buffer, orchestrator):
        self._frame_buffer = frame_buffer
        self._orchestrator = orchestrator

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def process_frame(
        self,
        frame,
        *,
        movement_event=None,
        weight_event=None,
        timestamp=None,
    ):
        """
        Process a single camera frame through the pipeline.

        Parameters
        ----------
        frame :
            Raw camera frame (e.g. a NumPy array).  The pipeline stores
            whatever object is passed; it does not inspect or copy it.
        movement_event : DetectionEvent or None
            Movement event produced by MOG2 (or a test stub).  When
            supplied, forwarded to EventOrchestrator.on_movement().
        weight_event : WeightEvent or None
            Weight-change event from the weight subsystem.  When
            supplied, forwarded to EventOrchestrator.on_weight().
        timestamp : datetime or None
            Capture timestamp for the frame.  When None, the
            FrameBuffer assigns datetime.now() automatically.

        Returns
        -------
        dict or None
            ``{"confirmed": <confirmed-event dict>, "frame": <frame or None>}``
            when the orchestrator produces a confirmed event, otherwise None.

            The ``"frame"`` value is None when the FrameBuffer contains no
            entry within its max_age window of the movement timestamp.
        """
        # 1. Always buffer the frame (with explicit or auto timestamp).
        self._frame_buffer.add(frame, timestamp=timestamp)

        confirmed = None

        # 2. Forward movement event, capturing any immediate confirmation.
        if movement_event is not None:
            confirmed = self._orchestrator.on_movement(movement_event)

        # 3. Forward weight event.  Weight may confirm an already-pending
        #    movement, so check this even when movement_event was supplied.
        if weight_event is not None:
            weight_result = self._orchestrator.on_weight(weight_event)
            # Weight result takes precedence only when movement didn't
            # already produce a confirmation this call.
            if confirmed is None:
                confirmed = weight_result

        # 4. If no confirmation yet, nothing further to do.
        if confirmed is None:
            return None

        # 5. Confirmed event — retrieve the frame closest to when the
        #    movement actually occurred (not the confirmation timestamp).
        movement_ts = confirmed.get("movement_timestamp")

        matched_frame = None
        if movement_ts is not None:
            entry = self._frame_buffer.get_closest_frame(movement_ts)
            if entry is not None:
                _ts, matched_frame = entry

        return {
            "confirmed": confirmed,
            "frame": matched_frame,
        }
