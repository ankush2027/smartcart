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
        # SDK errors may contain configuration details; retain only safe startup messages.
        from edge.runtime.cart_runtime import StartupError
        if isinstance(error, StartupError):
            logger.error('%s', error)
        else:
            logger.error('Runtime stopped (%s). Check devices, active session, model/catalog and Firebase configuration; do not replay uncertain events.', type(error).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
