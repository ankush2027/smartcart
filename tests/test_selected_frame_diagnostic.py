"""Selected image identity and isolation; synthetic frames, no physical event."""
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from edge.detection.event import DetectionEvent
from edge.weight.weight_event import WeightEvent
from edge.runtime import selected_frame_diagnostic as diagnostic


def components():
    t = datetime(2026, 1, 1)
    frames = [np.full((8, 8, 3), value, dtype=np.uint8) for value in (17, 99, 201, 220)]
    frames[2][2:6, 2:6] = 0  # Sharpest synthetic candidate at +0.5s.
    camera = SimpleNamespace(reads=0, released=False)
    def read():
        i = camera.reads
        camera.reads += 1
        assert i < 4, 'Diagnostic must exit after first correlation'
        return True, frames[i]
    camera.read = read
    camera.release = lambda: setattr(camera, 'released', True)
    motion = SimpleNamespace(selected=None)
    def process(frame, timestamp=None):
        if camera.reads == 3:
            motion.selected = frame
        if camera.reads == 1:
            return DetectionEvent(timestamp, 'MOVEMENT', (), 1000)
        return None
    motion.process = process
    weights = SimpleNamespace(closed=False)
    weights.poll = lambda: None if camera.reads != 2 else WeightEvent(t + timedelta(seconds=.4), 'ADD', 500, 550, 50)
    weights.close = lambda: setattr(weights, 'closed', True)
    detector = SimpleNamespace(calls=[])
    def detect(frame):
        detector.calls.append(frame)
        assert frame is motion.selected
        return []
    detector.detect = detect
    times = iter([t, t + timedelta(seconds=.4), t + timedelta(seconds=.5), t + timedelta(seconds=.8)])
    return dict(camera=camera, motion=motion, weights=weights, detector=detector, clock=lambda: next(times)), frames


@pytest.mark.parametrize('movement_only', [False, True])
def test_exact_post_movement_frame_saved_and_inferred_once(tmp_path, monkeypatch, caplog, movement_only):
    import logging
    caplog.set_level(logging.INFO)
    parts, frames = components()
    from edge.detection.frame_selector import FrameSelector
    select = FrameSelector.select
    selections = []
    def record_selection(self, buffer, timestamp):
        result = select(self, buffer, timestamp)
        selections.append(result)
        return result
    monkeypatch.setattr(FrameSelector, 'select', record_selection)
    if movement_only:
        parts['weights'] = None
        def forbidden(*args, **kwargs):
            pytest.fail('Movement-only mode must not use weight/correlation')
        monkeypatch.setattr(diagnostic, 'EventOrchestrator', forbidden)
        monkeypatch.setattr(diagnostic, 'MotionPipeline', forbidden)
        monkeypatch.setattr(diagnostic, 'StdinSimulatedWeightSource', forbidden)
        # Additional movements must not replace the first anchor or add requests.
        process = parts['motion'].process
        def repeated_movement(frame, timestamp=None):
            process(frame, timestamp=timestamp)
            return DetectionEvent(timestamp, 'MOVEMENT', (), 1000)
        parts['motion'].process = repeated_movement
    original = diagnostic.save_frame
    saved = []
    def save(frame, directory, timestamp):
        saved.append(frame)
        return original(frame, directory, timestamp)
    monkeypatch.setattr(diagnostic, 'save_frame', save)
    path = diagnostic.capture_one(**parts, output_dir=tmp_path / 'images', movement_only_trigger=movement_only)
    assert len(selections) == 1
    assert selections[0]['candidate_count'] == 3
    assert selections[0]['frame'] is saved[0]
    assert path.is_absolute() and path.suffix == '.png'
    assert np.array_equal(cv2.imread(str(path)), frames[2])
    assert saved[0] is parts['detector'].calls[0] is parts['motion'].selected
    assert len(parts['detector'].calls) == 1 and parts['camera'].reads == 4
    assert parts['camera'].released
    if not movement_only:
        assert parts['weights'].closed
    assert np.all(frames[0] == 17) and np.all(frames[1] == 99)
    assert 'Selected frame timestamp: 2026-01-01T00:00:00' in caplog.text
    assert 'Selected frame delta from movement: +0.500000s' in caplog.text
    if movement_only:
        assert 'weight/correlation bypassed' in caplog.text
        assert 'Correlated timestamps' not in caplog.text
    else:
        assert 'delta=0.400000s' in caplog.text


def test_existing_output_not_overwritten(tmp_path):
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    one = diagnostic.save_frame(frame, tmp_path, datetime(2026, 1, 1))
    two = diagnostic.save_frame(frame, tmp_path, datetime(2026, 1, 1))
    assert one != two and one.read_bytes() == two.read_bytes()


def test_output_error_prevents_inference_and_cleans_up(tmp_path):
    bad = tmp_path / 'file'; bad.write_text('preserve')
    parts, _ = components()
    with pytest.raises(FileExistsError):
        diagnostic.capture_one(**parts, output_dir=bad)
    assert not parts['detector'].calls
    assert parts['camera'].released and parts['weights'].closed
    assert bad.read_text() == 'preserve'


def test_encoding_error_does_not_infer(tmp_path, monkeypatch):
    monkeypatch.setattr(cv2, 'imencode', lambda *args: (False, None))
    parts, _ = components()
    with pytest.raises(RuntimeError, match='encode'):
        diagnostic.capture_one(**parts, output_dir=tmp_path)
    assert not parts['detector'].calls and not list(tmp_path.iterdir())
    assert parts['camera'].released and parts['weights'].closed


def test_diagnostic_dependency_graph_excludes_mutation_modules():
    script = '''
import sys
from edge.runtime import selected_frame_diagnostic
for name in sys.modules:
    assert not name.startswith(('edge.cart_session', 'edge.billing', 'edge.database', 'edge.firebase', 'edge.pairing')), name
'''
    subprocess.run([sys.executable, '-B', '-c', script], check=True)


def test_output_is_gitignored():
    subprocess.run(['git', 'check-ignore', '-q', str(diagnostic.OUTPUT_DIR / 'selected_frame_test.png')], check=True)


def test_missing_model_does_not_open_camera(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, 'open_camera', lambda *args: pytest.fail('camera opened'))
    assert diagnostic.main(['--model', str(tmp_path / 'absent.pt'), '--simulate-weight']) == 1


def test_movement_only_cli_has_no_stdin_and_announces_mode(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    model = tmp_path / 'model.pt'
    model.touch()
    monkeypatch.setattr(diagnostic, 'YOLODetector', lambda **kwargs: object())
    monkeypatch.setattr(diagnostic, 'MOG2Processor', lambda: object())
    monkeypatch.setattr(diagnostic, 'open_camera', lambda index: object())
    monkeypatch.setattr(diagnostic, 'StdinSimulatedWeightSource',
                        lambda *args: pytest.fail('stdin source constructed'))
    calls = []
    monkeypatch.setattr(diagnostic, 'capture_one', lambda **kwargs: calls.append(kwargs))
    assert diagnostic.main(['--model', str(model), '--movement-only-trigger']) == 0
    assert len(calls) == 1 and calls[0]['movement_only_trigger']
    assert calls[0]['weights'] is None
    assert 'MOVEMENT-ONLY FRAME DIAGNOSTIC' in caplog.text
    assert 'no weight input required' in caplog.text


def test_trigger_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        diagnostic.main(['--model', 'unused.pt', '--simulate-weight', '--movement-only-trigger'])
    assert error.value.code == 2


def test_movement_only_camera_failure_releases_without_inference(tmp_path):
    parts, _ = components()
    parts['weights'] = None
    parts['camera'].read = lambda: (False, None)
    with pytest.raises(RuntimeError, match='Camera frame capture failed'):
        diagnostic.capture_one(**parts, output_dir=tmp_path, movement_only_trigger=True)
    assert parts['camera'].released and not parts['detector'].calls
