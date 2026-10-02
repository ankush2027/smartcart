"""Capture one production-selected recognition frame, infer once, then exit.

No cart/session/database/Firebase dependencies. PNG preserves unannotated pixels.
"""
import argparse
from datetime import datetime
import logging
from pathlib import Path
from uuid import uuid4

from edge.detection.camera import open_camera
from edge.detection.mog2 import MOG2Processor
from edge.detection.frame_buffer import FrameBuffer
from edge.detection.motion_pipeline import MotionPipeline
from edge.orchestrator import EventOrchestrator
from edge.model.yolo_detector import YOLODetector
from edge.runtime.weight_source import StdinSimulatedWeightSource
from edge.runtime.diagnostics import log_exception
from edge.detection.frame_selector import DEFAULT_WINDOW_END
from edge.runtime.recognition_frames import RecognitionFrames, DEFAULT_RECOGNITION_DELAY, DEFAULT_RECOGNITION_WAIT

OUTPUT_DIR = Path(__file__).resolve().parents[2] / 'data' / 'diagnostics'
logger = logging.getLogger(__name__)


def save_frame(frame, output_dir, timestamp):
    """Lossless PNG, exclusive creation, never annotate or overwrite a file."""
    import cv2
    ok, encoded = cv2.imencode('.png', frame)
    if not ok:
        raise RuntimeError('Could not encode selected recognition frame')
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'selected_frame_{timestamp:%Y%m%d_%H%M%S_%f}_{uuid4().hex}.png'
    with path.open('xb') as target:
        target.write(encoded.tobytes())
    return path


def capture_one(*, camera, motion, weights=None, detector, output_dir=OUTPUT_DIR,
                clock=datetime.now, recognition_delay=DEFAULT_RECOGNITION_DELAY,
                recognition_wait=DEFAULT_RECOGNITION_WAIT, recognition_window_end=DEFAULT_WINDOW_END,
                movement_only_trigger=False):
    """Use the same production component ordering; never dispatch a cart job."""
    buffer = FrameBuffer()
    pipeline = None if movement_only_trigger else MotionPipeline(buffer, EventOrchestrator())
    movement_anchored = False
    selector = RecognitionFrames(delay=recognition_delay, max_wait=recognition_wait, window_end=recognition_window_end)
    try:
        while True:
            success, frame = camera.read()
            if not success or frame is None:
                raise RuntimeError('Camera frame capture failed')
            timestamp = clock()
            raw = frame.copy()
            movement = motion.process(raw, timestamp=timestamp)
            weight = None if movement_only_trigger else weights.poll()
            if movement is not None:
                logger.info('Movement received: %s', movement.timestamp.isoformat())
            if weight is not None:
                logger.info('Weight received: delta=%+g g direction=%s timestamp=%s',
                            weight.weight_change, weight.event_type, weight.timestamp.isoformat())
            if movement_only_trigger:
                buffer.add(raw, timestamp=timestamp)
                result = None
                if movement is not None and not movement_anchored:
                    movement_anchored = True
                    # Diagnostic-only adapter for RecognitionFrames' identity keys.
                    # No weight event or correlation is created or claimed.
                    result = {'confirmed': {
                        'movement_timestamp': movement.timestamp,
                        'weight_timestamp': movement.timestamp,
                        'diagnostic_trigger': 'movement-only',
                    }}
            else:
                result = pipeline.process_frame(raw, movement_event=movement,
                                                weight_event=weight, timestamp=timestamp)
            ready = selector.update(result, buffer)
            if not ready:
                continue
            result = ready[0]
            confirmed, selected = result['confirmed'], result['frame']
            if movement_only_trigger:
                logger.info('Diagnostic movement anchor: %s; weight/correlation bypassed',
                            confirmed['movement_timestamp'].isoformat())
            else:
                logger.info('Correlated timestamps: movement=%s weight=%s delta=%.6fs',
                            confirmed['movement_timestamp'].isoformat(),
                            confirmed['weight_timestamp'].isoformat(), confirmed['time_diff'])
            if selected is None:
                raise RuntimeError('Selection completed without a frame; no inference performed')
            selected_ts = result['frame_timestamp']
            delta = (selected_ts - confirmed['movement_timestamp']).total_seconds()
            logger.info('Candidate window: %s .. %s; count=%d', result['window_start'].isoformat(), result['window_end'].isoformat(), result['candidate_count'])
            logger.info('Candidate quality scores: %s', [(ts.isoformat(), score) for ts, score in result['candidate_scores']])
            logger.info('Selected frame quality score: %.6f', result['quality_score'])
            logger.info('Selected frame timestamp: %s', selected_ts.isoformat())
            logger.info('Selected frame delta from movement: %+.6fs', delta)
            path = save_frame(selected, output_dir, selected_ts)
            logger.info('Saved exact recognition frame: %s', path)
            detections = detector.detect(selected)
            logger.info('YOLO result: %s', [(d.yolo_class, d.confidence) for d in detections])
            return path
    finally:
        try:
            camera.release()
        finally:
            if weights is not None:
                weights.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera-index', type=int, default=0)
    parser.add_argument('--model', type=Path, required=True)
    trigger = parser.add_mutually_exclusive_group(required=True)
    trigger.add_argument('--simulate-weight', action='store_true')
    trigger.add_argument('--movement-only-trigger', action='store_true',
                         help='Diagnostic only: select after movement without weight/correlation')
    parser.add_argument('--initial-weight', type=float, default=500.0)
    parser.add_argument('--recognition-window-start', '--recognition-delay', dest='recognition_delay', type=float, default=DEFAULT_RECOGNITION_DELAY)
    parser.add_argument('--recognition-window-end', type=float, default=DEFAULT_WINDOW_END)
    parser.add_argument('--recognition-wait', type=float, default=DEFAULT_RECOGNITION_WAIT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        model = args.model.expanduser().resolve()
        if not model.is_file():
            raise FileNotFoundError(f'Model artifact missing: {model}')
        detector = YOLODetector(model_path=model)  # unchanged adapter threshold
        motion = MOG2Processor()
        weights = None if args.movement_only_trigger else StdinSimulatedWeightSource(args.initial_weight)
        try:
            camera = open_camera(args.camera_index)
        except BaseException:
            if weights is not None:
                weights.close()
            raise
        if args.movement_only_trigger:
            logger.info('MOVEMENT-ONLY FRAME DIAGNOSTIC — move product through camera; no weight input required.')
        else:
            logger.info('Diagnostic only: no session or billing. Enter signed gram deltas; Ctrl-C exits.')
        capture_one(camera=camera, motion=motion, weights=weights, detector=detector,
                    movement_only_trigger=args.movement_only_trigger,
                    recognition_delay=args.recognition_delay, recognition_wait=args.recognition_wait, recognition_window_end=args.recognition_window_end)
        return 0
    except KeyboardInterrupt:
        logger.info('Diagnostic stopped by user')
        return 130
    except Exception as error:
        log_exception(logger, 'Selected-frame diagnostic failed; no billing path exists.', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
