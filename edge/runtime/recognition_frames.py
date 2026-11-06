"""Bounded post-movement selection shared by runtime and image diagnostic.

Acquisition owns the frame buffer. The supervisor only inspects deadlines;
this lock protects pending metadata, never camera/inference/network work.
"""
from collections import deque
import logging
import math
from threading import RLock
from time import monotonic

from edge.detection.frame_selector import FrameSelector, DEFAULT_WINDOW_START, DEFAULT_WINDOW_END

DEFAULT_RECOGNITION_DELAY = DEFAULT_WINDOW_START
DEFAULT_RECOGNITION_WAIT = 1.5
logger = logging.getLogger(__name__)


class RecognitionFrames:
    def __init__(self, *, delay=DEFAULT_RECOGNITION_DELAY,
                 max_wait=DEFAULT_RECOGNITION_WAIT, capacity=8, clock=monotonic,
                 window_end=DEFAULT_WINDOW_END, selector=None):
        self.selector = selector or FrameSelector(window_start=delay, window_end=window_end)
        if not math.isfinite(max_wait) or max_wait <= self.selector.window_end:
            raise ValueError('Recognition wait must be finite and greater than window end')
        if capacity < 1:
            raise ValueError('Recognition pending capacity must be positive')
        self.delay, self.max_wait, self.capacity, self.clock = delay, max_wait, capacity, clock
        self._pending = deque()
        self._last_movement = self._last_weight = None
        self._lock = RLock()
        self._cancelled = False

    def check_timeout(self):
        with self._lock:
            if self._pending and self.clock() >= self._pending[0][2]:
                raise TimeoutError('Candidate selection deadline reached; event not dispatched')

    def cancel(self):
        with self._lock:
            self._pending.clear()
            self._cancelled = True

    def update(self, result, buffer):
        """Accept confirmations and return ready jobs once; never sleep or infer."""
        with self._lock:
            if self._cancelled:
                return []
            self.check_timeout()
            if result is not None:
                event = result['confirmed']
                movement, weight = event['movement_timestamp'], event['weight_timestamp']
                if ((self._last_movement is not None and movement <= self._last_movement)
                        or (self._last_weight is not None and weight <= self._last_weight)):
                    logger.info('Recognition selection ignored duplicate/out-of-order confirmation')
                else:
                    if len(self._pending) >= self.capacity:
                        raise RuntimeError('Recognition pending queue full; halt and reconcile cart')
                    self._last_movement, self._last_weight = movement, weight
                    start, end = self.selector.bounds(movement)
                    self._pending.append((event, end, self.clock() + self.max_wait))
                    logger.info('Recognition candidates pending: window=%s..%s deadline_in=%.3fs',
                                start.isoformat(), end.isoformat(), self.max_wait)
        ready = []
        while True:
            with self._lock:
                if self._cancelled or not self._pending:
                    return ready
                self.check_timeout()
                request = self._pending[0]
                event, end, _ = request
                frames = buffer.get_recent_frames()
                if not frames or max(ts for ts, _ in frames) < end:
                    return ready
            # Acquisition owns buffer; scoring never holds the supervisor lock.
            try:
                selection = self.selector.select(buffer, event['movement_timestamp'])
            except Exception:
                self.cancel()  # Fail closed: no retry or early-frame fallback.
                raise
            with self._lock:
                if self._cancelled:
                    return []
                self.check_timeout()
                self._pending.popleft()
                logger.info('Recognition frame selected: timestamp=%s movement_delta=%+.6fs quality=%.3f candidates=%d window=%s..%s',
                            selection['frame_timestamp'].isoformat(), selection['frame_delta'],
                            selection['quality_score'], selection['candidate_count'],
                            selection['window_start'].isoformat(), selection['window_end'].isoformat())
                ready.append({'confirmed': event, **selection})
