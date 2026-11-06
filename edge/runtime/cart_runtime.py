"""One acquisition loop and one serial recognition/billing worker; no retries."""
from dataclasses import dataclass
from datetime import datetime
import math
import logging
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Event, Thread
from time import monotonic

from edge.runtime.diagnostics import log_exception
from edge.runtime.recognition_frames import RecognitionFrames, DEFAULT_RECOGNITION_DELAY, DEFAULT_RECOGNITION_WAIT
from edge.detection.frame_selector import DEFAULT_WINDOW_END
from edge.cart_session import CartSession
from edge.billing.billing import CheckoutFrozenError
from edge.database.product_repository import get_active_products
from edge.detection.frame_buffer import FrameBuffer
from edge.detection.motion_pipeline import MotionPipeline
from edge.detection.product_resolver import ProductInfo, ProductResolver
from edge.orchestrator import EventOrchestrator
from edge.pairing.session_store import get_active_session_for_cart


logger = logging.getLogger(__name__)


class ShutdownError(RuntimeError):
    """Shutdown deadline expired or resource cleanup failed; do not restart in-process."""


class StartupError(ValueError):
    pass


def validate_model_path(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise StartupError(f'YOLO model artifact is missing: {path}')
    return path


def validated_catalog(class_names, *, demo_classes=None):
    """Build a billing catalog, requiring exact vocabulary unless explicitly subsetted."""
    names = tuple(class_names)
    if (not names or any(not isinstance(n, str) or not n.strip() for n in names)
            or len(set(names)) != len(names)):
        raise StartupError('Model vocabulary is empty or ambiguous')
    selected = None
    if demo_classes is not None:
        selected = tuple(demo_classes)
        if (not selected or any(not isinstance(n, str) or not n.strip() for n in selected)
                or len(set(selected)) != len(selected) or not set(selected).issubset(names)):
            raise StartupError('Demo classes must be unique model classes')
    catalog = []
    for pid, name, _barcode, price, weight, label in get_active_products():
        if (not isinstance(label, str) or not label.strip()
                or not isinstance(weight, (int, float)) or not math.isfinite(weight)
                or weight <= 0 or not isinstance(price, (int, float))
                or not math.isfinite(price) or price < 0):
            raise StartupError('Catalog requires valid unique classes, weights and prices')
        if selected is None or label in selected:
            catalog.append(ProductInfo(pid, label, name, price, weight))
    labels = [p.yolo_class for p in catalog]
    if len(set(labels)) != len(labels):
        raise StartupError('Model/catalog vocabulary mismatch or duplicate catalog class')
    if selected is None and set(labels) != set(names):
        raise StartupError('Model/catalog vocabulary mismatch or duplicate catalog class')
    if selected is not None and set(labels) != set(selected):
        raise StartupError('Demo classes must match active catalog classes exactly')
    return catalog


def resume_session(cart_id, detector, firebase_ref, *, demo_classes=None):
    if not isinstance(cart_id, str) or not cart_id.strip():
        raise StartupError('An explicit stable cart ID is required')
    catalog = validated_catalog(detector.class_names, demo_classes=demo_classes)
    meta = get_active_session_for_cart(cart_id.strip())
    if meta is None:
        raise StartupError('No active session for this cart; start one through the existing lifecycle')
    # Ownership is read from SQLite by existing sync. Never assign a UID here.
    try:
        return CartSession(ProductResolver(catalog), session_id=meta['session_id'],
                           firebase_ref=firebase_ref)
    except CheckoutFrozenError:
        raise StartupError('Session checkout is frozen; shopping cannot resume') from None


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    event_type: str
    detail: str = ''


class CartRuntime:
    def __init__(self, *, camera, motion, weights, detector, session, cart_id,
                 on_outcome=lambda outcome: None, queue_size=8, clock=datetime.now,
                 max_event_age=30.0, shutdown_timeout=5.0,
                 recognition_delay=DEFAULT_RECOGNITION_DELAY,
                 recognition_wait=DEFAULT_RECOGNITION_WAIT, selection_clock=monotonic,
                 recognition_window_end=DEFAULT_WINDOW_END, frame_selector=None):
        self.camera, self.motion, self.weights = camera, motion, weights
        self.detector, self.session, self.cart_id = detector, session, cart_id
        self.on_outcome = on_outcome
        self.clock = clock
        self.started_at = clock()
        if not math.isfinite(max_event_age) or max_event_age <= 0:
            raise ValueError('max_event_age must be positive and finite')
        self.max_event_age = max_event_age
        self._last_movement = None
        self._last_weight = None
        self.frame_buffer = FrameBuffer()
        self.pipeline = MotionPipeline(self.frame_buffer, EventOrchestrator())
        self.recognition_frames = RecognitionFrames(delay=recognition_delay, max_wait=recognition_wait,
                                                     capacity=queue_size, clock=selection_clock,
                                                     window_end=recognition_window_end, selector=frame_selector)
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.jobs = Queue(maxsize=queue_size)
        self.stopping = Event()
        self.failed = Event()
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            raise ValueError('shutdown_timeout must be positive and finite')
        self.shutdown_timeout = shutdown_timeout
        self._worker = None
        self._acquisition = None

    def acquire(self, frame, timestamp=None):
        """Acquisition-only step; inference and Firebase never execute here."""
        timestamp = timestamp or self.clock()
        # Retain an unannotated, independent frame even if a camera reuses memory.
        raw = frame.copy()
        was_active = getattr(self.motion, 'movement_active', False)
        movement = self.motion.process(raw, timestamp=timestamp)
        if was_active and not getattr(self.motion, 'movement_active', False):
            logger.info('Motion reset: detector rearmed')
        if movement is not None:
            logger.info('Movement received: timestamp=%s; detector latched until reset', movement.timestamp.isoformat())
        weight = self.weights.poll()
        if weight is not None:
            logger.info('Weight received: delta=%+g g direction=%s timestamp=%s',
                        weight.weight_change, weight.event_type, weight.timestamp.isoformat())
        result = self.pipeline.process_frame(raw, movement_event=movement,
                                             weight_event=weight, timestamp=timestamp)
        if self.stopping.is_set():
            return
        for result in self.recognition_frames.update(result, self.frame_buffer):
            if self.stopping.is_set():
                return
            try:
                self.jobs.put_nowait(result)
                logger.info('Dispatch job queued: %s frame_present=%s', result['confirmed']['event_type'], result['frame'] is not None)
            except Full:
                self.failed.set()
                self.stopping.set()
                raise RuntimeError('Recognition queue full; halt and reconcile cart')

    def dispatch(self, event):
        """Consume one confirmation exactly once. Never retry a mutation."""
        confirmed = event['confirmed']
        kind, delta = confirmed.get('event_type'), confirmed.get('weight_change')
        if (confirmed.get('confirmed') is not True or kind not in ('ADD', 'REMOVE')
                or not isinstance(delta, (int, float)) or not math.isfinite(delta)
                or (kind == 'ADD' and delta <= 0) or (kind == 'REMOVE' and delta >= 0)):
            return DispatchOutcome('rejected', str(kind), 'Invalid confirmation')
        movement_ts = confirmed.get('movement_timestamp')
        weight_ts = confirmed.get('weight_timestamp')
        if not isinstance(movement_ts, datetime) or not isinstance(weight_ts, datetime):
            return DispatchOutcome('rejected', kind, 'Missing event identity timestamps')
        if ((self._last_movement is not None and movement_ts <= self._last_movement)
                or (self._last_weight is not None and weight_ts <= self._last_weight)):
            return DispatchOutcome('ignored', kind, 'Duplicate or out-of-order event')
        now = self.clock()
        if (min(movement_ts, weight_ts) < self.started_at
                or min(movement_ts, weight_ts) > now
                or max(movement_ts, weight_ts) > now
                or (now - min(movement_ts, weight_ts)).total_seconds() > self.max_event_age):
            return DispatchOutcome('rejected', kind, 'Stale or future event; reconcile cart')
        # Consume before recognition/mutation, including downstream failure.
        # This is dispatch replay protection, not a new confirmation algorithm.
        self._last_movement, self._last_weight = movement_ts, weight_ts
        # Recheck authoritative cart/session selection at dispatch time.
        meta = get_active_session_for_cart(self.cart_id)
        if meta is None or meta['session_id'] != self.session.session_id:
            return DispatchOutcome('rejected', kind, 'Active session changed')
        frame = event['frame']
        if kind == 'ADD' and frame is None:
            return DispatchOutcome('unresolved', kind, 'No matched frame')
        try:
            detections = self.detector.detect(frame) if frame is not None else []
        except Exception as error:
            log_exception(logger, 'Recognition failed; nothing billed.', error)
            return DispatchOutcome('rejected', kind, 'Recognition failed; nothing billed')
        logger.info('Recognition result: %s', [(d.yolo_class, round(d.confidence, 4)) for d in detections])
        if self.failed.is_set():
            return DispatchOutcome('rejected', kind, 'Runtime halted before billing')
        try:
            method = self.session.process_add if kind == 'ADD' else self.session.process_remove
            result = method(detections, delta)
        except Exception as error:
            log_exception(logger, 'CartSession failed; do not replay uncertain events.', error)
            if self.session.last_mutation_committed:
                return DispatchOutcome('committed_sync_failed', kind,
                                       'Local mutation committed; do not replay; reconcile Firebase')
            return DispatchOutcome('rejected', kind, 'Local mutation failed; reconcile before resuming')
        logger.info('Resolver/session result: direction=%s resolved=%s success=%s reason=%s',
                    kind, result.resolved, result.success, result.reason)
        if result.success:
            return DispatchOutcome('committed', kind)
        return DispatchOutcome('unresolved' if not result.resolved else 'rejected',
                               kind, result.reason)

    def _work(self):
        while True:
            try:
                job = self.jobs.get(timeout=0.1)
            except Empty:
                if self.stopping.is_set():
                    return
                continue
            try:
                if self.failed.is_set():
                    continue  # fail-stop: never apply queued events after uncertainty
                try:
                    outcome = self.dispatch(job)
                except Exception as error:
                    log_exception(logger, 'Dispatch failed; do not replay uncertain events.', error)
                    outcome = DispatchOutcome('rejected', 'UNKNOWN', 'Dispatch failed; reconcile cart')
                logger.info('Billing/dispatch outcome: %s status=%s detail=%s',
                            outcome.event_type, outcome.status, outcome.detail)
                # Stop after any unresolved/rejected/partial event: physical and
                # billed contents may now differ. No guessed continuation.
                if outcome.status not in ('committed', 'ignored'):
                    self.failed.set()
                    self.stopping.set()
                try:
                    self.on_outcome(outcome)
                except Exception as error:
                    log_exception(logger, 'Outcome reporting failed; do not replay uncertain events.', error)
                    self.failed.set()
                    self.stopping.set()
            finally:
                self.jobs.task_done()

    def stop(self):
        self.stopping.set()
        self.recognition_frames.cancel()

    def run(self):
        if self._worker is not None:
            raise RuntimeError('Runtime instances cannot be restarted')
        errors = Queue()

        def acquire_loop():
            try:
                while not self.stopping.is_set():
                    success, frame = self.camera.read()
                    if self.stopping.is_set():
                        return
                    if not success or frame is None:
                        raise RuntimeError('Camera capture failed; runtime stopped')
                    if not self.stopping.is_set():
                        self.acquire(frame)
            except BaseException as error:
                errors.put(error)
                self.failed.set()
                self.stop()

        # Daemon threads cannot hold process exit hostage to third-party calls.
        # They are never forcibly terminated or replaced inside this process.
        self._worker = Thread(target=self._work, name='cart-dispatch', daemon=True)
        self._acquisition = Thread(target=acquire_loop, name='cart-acquisition', daemon=True)
        self._worker.start()
        self._acquisition.start()
        try:
            while not self.stopping.wait(0.1):
                self.recognition_frames.check_timeout()
        except BaseException:
            self.failed.set()
            raise
        finally:
            self.stop()
            deadline = monotonic() + self.shutdown_timeout
            cleanup_errors = Queue()

            def close_resource(close):
                try:
                    close()
                except BaseException as error:
                    log_exception(logger, 'Resource cleanup failed.', error)
                    cleanup_errors.put(error)

            # Release can unblock capture, but release itself may be native/blocking.
            cleanup = [Thread(target=close_resource, args=(close,), daemon=True,
                              name=name) for close, name in (
                                  (self.camera.release, 'cart-camera-close'),
                                  (self.weights.close, 'cart-weight-close'))]
            for thread in cleanup:
                thread.start()
            owned = [self._acquisition, self._worker, *cleanup]
            for thread in owned:
                thread.join(max(0.0, deadline - monotonic()))
            if any(thread.is_alive() for thread in owned) or not cleanup_errors.empty():
                self.failed.set()
                raise ShutdownError('Shutdown incomplete; exit this process and reconcile before restart')
        if not errors.empty():
            raise errors.get_nowait()
