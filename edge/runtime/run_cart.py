"""Run explicit software/development cart integration, separate from pairing API."""
import argparse
from contextlib import contextmanager
import hashlib
import logging
from pathlib import Path
import signal
import tempfile

from edge.database.database import DATABASE_PATH, initialize_database
from edge.firebase.client import initialize, get_reference
from edge.model.yolo_detector import DEFAULT_MODEL_PATH, YOLODetector
from edge.runtime.cart_runtime import CartRuntime, ShutdownError, resume_session, validate_model_path
from edge.runtime.weight_source import StdinSimulatedWeightSource
from edge.runtime.diagnostics import log_exception
from edge.detection.frame_selector import DEFAULT_WINDOW_END
from edge.runtime.recognition_frames import DEFAULT_RECOGNITION_DELAY, DEFAULT_RECOGNITION_WAIT


_quarantined_locks = []  # Retain ownership after an uncertain shutdown until process exit.


@contextmanager
def cart_lock(cart_id):
    """One cooperating runtime per database/cart on this Unix host."""
    import fcntl
    key = hashlib.sha256(f'{DATABASE_PATH.resolve()}:{cart_id}'.encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f'smartcart-runtime-{key}.lock'
    handle = path.open('a')
    quarantined = False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A runtime already owns this cart') from None
        try:
            yield
        except ShutdownError:
            # A timed-out writer could still be inside SQLite/Firebase. Do not
            # hand this cart to a replacement runtime in the same live process.
            _quarantined_locks.append(handle)
            quarantined = True
            raise
    finally:
        if not quarantined:
            handle.close()
    # Do not unlink: replacing a lock inode permits two concurrent owners.


def main(argv=None):
    parser = argparse.ArgumentParser(description='SmartCart software runtime — simulated weight only')
    parser.add_argument('--cart-id', required=True)
    parser.add_argument('--model', default=str(DEFAULT_MODEL_PATH))
    parser.add_argument('--initial-weight', type=float, default=500.0)
    parser.add_argument('--camera-index', type=int, default=0)
    parser.add_argument('--simulate-weight', action='store_true', required=True,
                        help='Explicit development mode: enter signed gram changes on stdin')
    parser.add_argument('--recognition-window-start', '--recognition-delay', dest='recognition_delay', type=float, default=DEFAULT_RECOGNITION_DELAY)
    parser.add_argument('--recognition-window-end', type=float, default=DEFAULT_WINDOW_END)
    parser.add_argument('--recognition-wait', type=float, default=DEFAULT_RECOGNITION_WAIT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    cart_id = args.cart_id.strip()
    if not cart_id:
        parser.error('--cart-id must not be blank')
    try:
        model_path = validate_model_path(args.model)
        with cart_lock(cart_id):
            detector = YOLODetector(model_path=model_path)
            initialize_database()
            # Validate catalog/session before initializing external Firebase or camera.
            session = resume_session(cart_id, detector, firebase_ref=None)
            initialize()
            session = resume_session(cart_id, detector, firebase_ref=get_reference('/'))
            from edge.detection.camera import open_camera
            from edge.detection.mog2 import MOG2Processor
            motion = MOG2Processor()
            weights = StdinSimulatedWeightSource(args.initial_weight)
            camera = open_camera(args.camera_index)
            runtime = CartRuntime(camera=camera, motion=motion, weights=weights,
                                  detector=detector, session=session, cart_id=cart_id,
                                  recognition_delay=args.recognition_delay, recognition_wait=args.recognition_wait, recognition_window_end=args.recognition_window_end,
                                  on_outcome=lambda result: logger.info(
                                      'Event %s: %s (%s)', result.event_type,
                                      result.status, result.detail))
            logger.warning('SIMULATED WEIGHT — no physical sensor. Enter signed gram deltas; Ctrl-C stops.')
            previous = {sig: signal.signal(sig, lambda *_: runtime.stop())
                        for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                runtime.run()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            if runtime.failed.is_set():
                raise RuntimeError('Runtime halted; inspect/reconcile cart before restarting')
    except Exception as error:
        # Expected startup errors stay concise; unexpected failures retain safe traces.
        from edge.runtime.cart_runtime import StartupError
        if isinstance(error, StartupError):
            logger.error('%s', error)
        else:
            log_exception(logger, 'Runtime stopped; do not replay uncertain events.', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
