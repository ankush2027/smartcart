"""Run explicit software/development cart integration, separate from pairing API."""
import argparse
from contextlib import contextmanager
import errno
import hashlib
import logging
import os
from pathlib import Path
import signal
import tempfile

from edge.database.database import DATABASE_PATH, initialize_database
from edge.firebase.client import initialize, get_reference
from edge.model.yolo_detector import DEFAULT_MODEL_PATH, YOLODetector
from edge.runtime.cart_runtime import CartRuntime, ShutdownError, resume_session, validate_model_path
from edge.runtime.weight_source import create_weight_source
from edge.runtime.diagnostics import log_exception
from edge.detection.frame_selector import DEFAULT_WINDOW_END
from edge.runtime.recognition_frames import DEFAULT_RECOGNITION_DELAY, DEFAULT_RECOGNITION_WAIT


_quarantined_locks = []  # Retain ownership after an uncertain shutdown until process exit.


@contextmanager
def cart_lock(cart_id):
    """One cooperating runtime per database/cart on this host."""
    key = hashlib.sha256(f'{DATABASE_PATH.resolve()}:{cart_id}'.encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f'smartcart-runtime-{key}.lock'
    handle = path.open('a')
    quarantined = False
    try:
        if os.name == 'nt':
            import msvcrt
            # Every owner locks the same byte, even when the file is empty.
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise RuntimeError('A runtime already owns this cart') from None
        else:
            import fcntl
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


def build_parser():
    parser = argparse.ArgumentParser(description='SmartCart camera and cart runtime')
    parser.add_argument('--cart-id', required=True)
    parser.add_argument('--model', default=str(DEFAULT_MODEL_PATH))
    parser.add_argument('--demo-classes', metavar='CLASS[,CLASS...]',
                        help='Explicit billing allowlist for a demo subset; normal catalog validation remains strict when omitted')
    parser.add_argument('--initial-weight', type=float, default=500.0,
                        help='Initial grams for simulated stdin input only')
    parser.add_argument('--camera-index', type=int, default=0)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--simulate-weight', action='store_true',
                        help='Use explicit development input: signed gram deltas on stdin')
    source.add_argument('--serial-port', metavar='PORT',
                        help='Use physical HX711 serial input; never falls back to simulation')
    parser.add_argument('--serial-baudrate', type=int, default=115200)
    parser.add_argument('--serial-timeout', type=float, default=0.1)
    parser.add_argument('--tare-empty', action='store_true',
                        help='confirm the cart platform is empty and tare the serial sensor at startup')
    parser.add_argument('--max-weight-grams', type=float, default=20_000.0)
    parser.add_argument('--recognition-window-start', '--recognition-delay', dest='recognition_delay', type=float, default=DEFAULT_RECOGNITION_DELAY)
    parser.add_argument('--recognition-window-end', type=float, default=DEFAULT_WINDOW_END)
    parser.add_argument('--recognition-wait', type=float, default=DEFAULT_RECOGNITION_WAIT)
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serial_port and not args.tare_empty:
        parser.error('--serial-port requires --tare-empty because opening the Mega may reset it')
    if args.simulate_weight and args.tare_empty:
        parser.error('--tare-empty is valid only with --serial-port')
    if not args.cart_id.strip():
        parser.error('--cart-id must not be blank')
    return args


def select_weight_source(args):
    if args.simulate_weight:
        return create_weight_source('stdin', initial_weight=args.initial_weight)
    return create_weight_source('serial', port=args.serial_port, baudrate=args.serial_baudrate,
                                timeout=args.serial_timeout, max_weight_grams=args.max_weight_grams)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    cart_id = args.cart_id.strip()
    demo_classes = None
    if args.demo_classes is not None:
        demo_classes = tuple(label.strip() for label in args.demo_classes.split(','))
        if (not demo_classes or any(not label for label in demo_classes)
                or len(set(demo_classes)) != len(demo_classes)):
            logger.error('--demo-classes must be a comma-separated list of unique nonempty model class names')
            return 1
    try:
        model_path = validate_model_path(args.model)
        with cart_lock(cart_id):
            detector = YOLODetector(model_path=model_path)
            initialize_database()
            # Validate catalog/session before initializing external Firebase or camera.
            session = resume_session(cart_id, detector, firebase_ref=None,
                                     demo_classes=demo_classes)
            initialize()
            session = resume_session(cart_id, detector, firebase_ref=get_reference('/'),
                                     demo_classes=demo_classes)
            from edge.detection.camera import open_camera
            from edge.detection.mog2 import MOG2Processor
            motion = MOG2Processor()
            weights = select_weight_source(args)
            try:
                if args.serial_port:
                    confirmation = input('Confirm platform is EMPTY; type TARE to tare sensor: ')
                    if confirmation != 'TARE':
                        raise RuntimeError('Physical sensor tare cancelled')
                    weights.tare()
                camera = open_camera(args.camera_index)
            except BaseException:
                weights.close()
                raise
            runtime = CartRuntime(camera=camera, motion=motion, weights=weights,
                                  detector=detector, session=session, cart_id=cart_id,
                                  recognition_delay=args.recognition_delay, recognition_wait=args.recognition_wait, recognition_window_end=args.recognition_window_end,
                                  on_outcome=lambda result: logger.info(
                                      'Event %s: %s (%s)', result.event_type,
                                      result.status, result.detail))
            if args.simulate_weight:
                logger.warning('SIMULATED WEIGHT — no physical sensor. Enter signed gram deltas; Ctrl-C stops.')
            else:
                logger.info('Physical HX711 input selected on %s at %d baud; no simulation fallback.',
                            args.serial_port, args.serial_baudrate)
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
