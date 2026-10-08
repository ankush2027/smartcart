"""Explicit local console fixtures: synthetic motion/vision, real billing path.

No camera, YOLO, HTTP simulation endpoint or physical sensor is enabled here.
Commands carry the session AND checkout generation to prevent stale input from
being transferred to a new runtime. Only the runtime worker performs mutations.
"""
from queue import Empty, Full, Queue
from threading import Event, Lock
import shlex
import sys

import numpy as np

from edge.billing.billing import get_bill
from edge.database.product_repository import get_active_products
from edge.detection.event import DetectionEvent
from edge.detection.product_resolver import Detection
from edge.runtime.cart_runtime import CartRuntime, resume_session, validated_catalog
from edge.runtime.weight_source import SimulatedWeightSource


class SoftwareConsole:
    def __init__(self, output=print):
        self.output = output
        self._lock = Lock()
        self._runtime = None

    def bind(self, runtime):
        with self._lock:
            if self._runtime is not None:
                raise RuntimeError('Demo console already attached')
            self._runtime = runtime
        self.output(f'DEMO INPUT session={runtime.session_id} generation={runtime.generation}')
        self.output('Commands: ADD <session> <generation> "<catalog label>"; REMOVE likewise; STATUS')
        self.output('Supported labels: ' + ', '.join(runtime.products))

    def unbind(self, runtime):
        with self._lock:
            if self._runtime is runtime:
                self._runtime = None

    def submit(self, line):
        try:
            parts = shlex.split(line)
            with self._lock:
                runtime = self._runtime
                if runtime is None or runtime.stopping.is_set():
                    raise ValueError('No shopping runtime; wait for pairing/resume')
                if parts == ['STATUS']:
                    self.output(f'DEMO INPUT session={runtime.session_id} generation={runtime.generation}')
                    return True
                if (len(parts) != 4 or parts[0] not in ('ADD', 'REMOVE')
                        or parts[1:3] != [str(runtime.session_id), str(runtime.generation)]
                        or parts[3] not in runtime.products):
                    raise ValueError('Use current session/generation and a supported catalog label')
                runtime.commands.put_nowait((parts[0], parts[3]))
            return True
        except (ValueError, Full):
            self.output('Demo input rejected: check STATUS, command, label or queue capacity')
            return False

    def read(self, stream=None):
        # Blocking readline is isolated in one daemon thread, portable to Windows.
        # No stdin/select polling occurs in acquisition or supervisor threads.
        for line in stream if stream is not None else sys.stdin:
            if line.strip():
                self.submit(line)


class _FixtureDetector:
    def __init__(self):
        self.class_names = tuple(row[5] for row in get_active_products())
        self.label = None

    def detect(self, frame):
        return [Detection(self.label, 0.99)]


class SoftwareDemoRuntime:
    shutdown_timeout = 5.0

    def __init__(self, *, cart_id, session_id, firebase_ref, console):
        self.console = console
        detector = _FixtureDetector()
        catalog = validated_catalog(detector.class_names)
        session = resume_session(cart_id, detector, firebase_ref)
        if session.session_id != session_id:
            raise RuntimeError('Active session changed before demo attachment')
        self.session_id = session_id
        self.generation = session.checkout_generation
        # Preserve existing 10g event threshold; never invent hardware calibration.
        self.products = {p.yolo_class: p for p in catalog if p.expected_weight >= 10}
        by_id = {p.product_id: p for p in catalog}
        initial = 500 + sum(by_id[row[0]].expected_weight * row[2] for row in get_bill(session_id))
        weights = SimulatedWeightSource(initial)
        self.core = CartRuntime(camera=None, motion=None, weights=weights,
                                detector=detector, session=session, cart_id=cart_id)
        self.stopping = Event()
        self.failed = Event()
        self.commands = Queue(maxsize=8)

    def process(self, kind, label):
        """Worker-only step, also exercised synchronously by offline tests."""
        if self.stopping.is_set():
            raise RuntimeError('Demo runtime stopped')
        product = self.products[label]
        if kind not in ('ADD', 'REMOVE'):
            raise ValueError('Invalid demo direction')
        self.core.detector.label = label
        self.core.weights.inject_delta(product.expected_weight * (1 if kind == 'ADD' else -1))
        weight = self.core.weights.poll()
        movement = DetectionEvent(weight.timestamp, 'MOVEMENT', (0, 0, 8, 8), 64)
        result = self.core.pipeline.process_frame(
            np.zeros((8, 8, 3), dtype=np.uint8), movement_event=movement,
            weight_event=weight, timestamp=weight.timestamp)
        if result is None:
            raise RuntimeError('Demo event was not confirmed')
        outcome = self.core.dispatch(result)
        # Uncertain/rejected events must not be replayed or followed by queued work.
        if outcome.status != 'committed':
            self.failed.set()
            self.stop()
        return outcome

    def run(self):
        self.console.bind(self)
        try:
            while not self.stopping.is_set():
                try:
                    kind, label = self.commands.get(timeout=0.1)
                except Empty:
                    continue
                if self.stopping.is_set():
                    break
                outcome = self.process(kind, label)
                self.console.output(f'DEMO {kind}: {outcome.status}; {outcome.detail}')
        finally:
            self.stop()
            self.console.unbind(self)

    def stop(self):
        self.stopping.set()
