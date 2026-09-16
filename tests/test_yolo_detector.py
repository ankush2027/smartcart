"""
Unit tests for YOLODetector.

Design principles:
    - No best.pt required — every test uses a fake injected model.
    - No internet, no camera, no GPU, no Raspberry Pi.
    - No actual Ultralytics inference is performed.
    - All tests are deterministic.

Fake-model strategy:
    YOLODetector.__init__ accepts an optional `_model` argument.
    When supplied, the constructor skips the `ultralytics` import entirely
    and uses the injected object as the model.

    Fake objects mimic only the Ultralytics attributes that YOLODetector
    actually accesses:
        result.names   — dict {int: str}
        result.boxes   — iterable of box objects
        box.cls[0]     — integer class ID
        box.conf[0]    — float confidence

Covered cases (per task spec):
     1. Empty detections return [].
     2. One detection → exactly one Detection.
     3. Correct class ID → correct class name.
     4. Correct confidence is returned.
     5. Multiple detections → multiple Detection objects.
     6. Confidence below 0.25 is excluded.
     7. Confidence exactly 0.25 is included.
     8. Confidence above 0.25 is included.
     9. Two detections of same class are NOT deduplicated.
    10. All six class IDs map to the correct class names.
    11. Custom confidence threshold works.
    12. threshold=0.0 allows all valid detections through.
    13. threshold=1.0 rejects detections below 1.0.
    14. Every returned item is an instance of the existing Detection dataclass.
    15. The model callable receives the exact frame passed to detect().
    16. Module import does not require loading a real YOLO model.
    17. Constructor with injected fake model does not import ultralytics.
"""

import importlib
import sys

import pytest

from edge.detection.product_resolver import Detection
from edge.model.yolo_detector import (
    DEFAULT_INFERENCE_THRESHOLD,
    DEFAULT_MODEL_PATH,
    YOLODetector,
)


# ---------------------------------------------------------------------------
# Fake Ultralytics result objects
# ---------------------------------------------------------------------------

class _FakeBox:
    """
    Minimal fake of an Ultralytics Results.boxes entry.

    Only the attributes accessed by YOLODetector are implemented:
        box.cls[0]   — integer class ID
        box.conf[0]  — float confidence
    """

    def __init__(self, cls_id: int, conf: float) -> None:
        self.cls  = [cls_id]
        self.conf = [conf]


class _FakeResult:
    """
    Minimal fake of a single Ultralytics Results object.

    Attributes
    ----------
    names : dict[int, str]
        Class-ID-to-name mapping (mirrors model.names in real Ultralytics).
    boxes : list[_FakeBox]
        Detected bounding boxes for this result.
    """

    def __init__(self, boxes: list[_FakeBox], names: dict) -> None:
        self.boxes = boxes
        self.names = names


class _FakeModel:
    """
    Callable fake that replaces a real `ultralytics.YOLO` instance.

    Returns a list containing one _FakeResult when called.
    Optionally records the frame it was called with.
    """

    def __init__(self, fake_result: _FakeResult) -> None:
        self._result = fake_result
        self.last_frame = None   # populated on each call, for assertion

    def __call__(self, frame):
        self.last_frame = frame
        return [self._result]


# ---------------------------------------------------------------------------
# The 6 trained class names — used across multiple tests
# ---------------------------------------------------------------------------

NAMES = {
    0: "1 to 3 Noodles Rs 10",
    1: "1 to 3 Noodles Rs 5",
    2: "Bru",
    3: "Coca Cola",
    4: "Lays",
    5: "Lifebuoy",
}


def _make_detector(boxes: list[_FakeBox], threshold: float = DEFAULT_INFERENCE_THRESHOLD) -> YOLODetector:
    """Helper: build a YOLODetector with a fake model containing `boxes`."""
    fake_result = _FakeResult(boxes=boxes, names=NAMES)
    fake_model  = _FakeModel(fake_result)
    return YOLODetector(_model=fake_model, confidence_threshold=threshold)


# ---------------------------------------------------------------------------
# Test 1 — Empty detections return []
# ---------------------------------------------------------------------------

def test_empty_detections_returns_empty_list():
    """No boxes → detect() returns an empty list (never None)."""
    detector = _make_detector(boxes=[])
    result = detector.detect("any-frame")
    assert result == []
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Test 2 — One detection → exactly one Detection
# ---------------------------------------------------------------------------

def test_single_detection_returns_one_item():
    """Exactly one box above threshold → list with exactly one Detection."""
    detector = _make_detector([_FakeBox(cls_id=3, conf=0.90)])
    result = detector.detect("frame")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test 3 — Correct class ID → correct class name
# ---------------------------------------------------------------------------

def test_class_id_converted_to_class_name():
    """Class ID 3 must produce yolo_class='Coca Cola', not an integer."""
    detector = _make_detector([_FakeBox(cls_id=3, conf=0.90)])
    det = detector.detect("frame")[0]
    assert det.yolo_class == "Coca Cola"
    assert not isinstance(det.yolo_class, int)


# ---------------------------------------------------------------------------
# Test 4 — Correct confidence is returned
# ---------------------------------------------------------------------------

def test_confidence_value_preserved():
    """The confidence stored in Detection matches the model's output."""
    conf_value = 0.82
    detector = _make_detector([_FakeBox(cls_id=4, conf=conf_value)])
    det = detector.detect("frame")[0]
    assert abs(det.confidence - conf_value) < 1e-9


# ---------------------------------------------------------------------------
# Test 5 — Multiple detections → multiple Detection objects
# ---------------------------------------------------------------------------

def test_multiple_boxes_produce_multiple_detections():
    """Three boxes → three Detection objects in the result list."""
    boxes = [
        _FakeBox(cls_id=2, conf=0.80),
        _FakeBox(cls_id=3, conf=0.70),
        _FakeBox(cls_id=4, conf=0.65),
    ]
    detector = _make_detector(boxes)
    result = detector.detect("frame")
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Test 6 — Confidence below 0.25 is excluded
# ---------------------------------------------------------------------------

def test_confidence_below_threshold_excluded():
    """Box with conf < DEFAULT threshold (0.25) must NOT appear in result."""
    detector = _make_detector([_FakeBox(cls_id=0, conf=0.24)])
    result = detector.detect("frame")
    assert result == []


# ---------------------------------------------------------------------------
# Test 7 — Confidence exactly 0.25 is included
# ---------------------------------------------------------------------------

def test_confidence_exactly_at_threshold_included():
    """Box with conf == threshold must be INCLUDED (>= not >)."""
    detector = _make_detector([_FakeBox(cls_id=1, conf=0.25)])
    result = detector.detect("frame")
    assert len(result) == 1
    assert abs(result[0].confidence - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# Test 8 — Confidence above 0.25 is included
# ---------------------------------------------------------------------------

def test_confidence_above_threshold_included():
    """Box with conf > threshold is included."""
    detector = _make_detector([_FakeBox(cls_id=5, conf=0.26)])
    result = detector.detect("frame")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test 9 — Same-class detections are NOT deduplicated
# ---------------------------------------------------------------------------

def test_same_class_boxes_not_deduplicated():
    """
    Two boxes with the same class ID → two Detection objects.
    YOLODetector must NOT deduplicate — that is ProductResolver's job.
    """
    boxes = [
        _FakeBox(cls_id=3, conf=0.80),
        _FakeBox(cls_id=3, conf=0.75),
    ]
    detector = _make_detector(boxes)
    result = detector.detect("frame")
    assert len(result) == 2
    assert result[0].yolo_class == "Coca Cola"
    assert result[1].yolo_class == "Coca Cola"


# ---------------------------------------------------------------------------
# Test 10 — All six class IDs map correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls_id, expected_name", [
    (0, "1 to 3 Noodles Rs 10"),
    (1, "1 to 3 Noodles Rs 5"),
    (2, "Bru"),
    (3, "Coca Cola"),
    (4, "Lays"),
    (5, "Lifebuoy"),
])
def test_all_six_class_ids_map_to_correct_names(cls_id, expected_name):
    """Each of the 6 trained class IDs must produce the correct class name."""
    detector = _make_detector([_FakeBox(cls_id=cls_id, conf=0.90)])
    result = detector.detect("frame")
    assert len(result) == 1
    assert result[0].yolo_class == expected_name


# ---------------------------------------------------------------------------
# Test 11 — Custom confidence threshold
# ---------------------------------------------------------------------------

def test_custom_threshold_filters_correctly():
    """
    A custom threshold (e.g. 0.60) should filter out boxes below it
    while including boxes at or above it.
    """
    boxes = [
        _FakeBox(cls_id=2, conf=0.59),   # below custom threshold → excluded
        _FakeBox(cls_id=3, conf=0.60),   # exactly at threshold → included
        _FakeBox(cls_id=4, conf=0.90),   # above threshold → included
    ]
    detector = _make_detector(boxes, threshold=0.60)
    result = detector.detect("frame")
    assert len(result) == 2
    class_names = {d.yolo_class for d in result}
    assert "Bru" not in class_names
    assert "Coca Cola" in class_names
    assert "Lays" in class_names


# ---------------------------------------------------------------------------
# Test 12 — threshold=0.0 passes all detections
# ---------------------------------------------------------------------------

def test_threshold_zero_passes_all():
    """
    With confidence_threshold=0.0 every box (including near-zero conf) passes.
    """
    boxes = [
        _FakeBox(cls_id=0, conf=0.0),
        _FakeBox(cls_id=1, conf=0.01),
        _FakeBox(cls_id=2, conf=0.99),
    ]
    detector = _make_detector(boxes, threshold=0.0)
    result = detector.detect("frame")
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Test 13 — threshold=1.0 rejects all detections
# ---------------------------------------------------------------------------

def test_threshold_one_rejects_all():
    """
    With confidence_threshold=1.0 no box passes (conf < 1.0 for any real det).
    """
    boxes = [
        _FakeBox(cls_id=3, conf=0.99),
        _FakeBox(cls_id=4, conf=0.50),
    ]
    detector = _make_detector(boxes, threshold=1.0)
    result = detector.detect("frame")
    assert result == []


# ---------------------------------------------------------------------------
# Test 14 — Every returned item is an instance of Detection
# ---------------------------------------------------------------------------

def test_all_returned_items_are_detection_instances():
    """
    Every element of the returned list must be an instance of the
    existing Detection dataclass from edge.detection.product_resolver.
    """
    boxes = [
        _FakeBox(cls_id=0, conf=0.80),
        _FakeBox(cls_id=5, conf=0.90),
    ]
    detector = _make_detector(boxes)
    result = detector.detect("frame")
    assert len(result) == 2
    for item in result:
        assert isinstance(item, Detection), (
            f"Expected Detection instance, got {type(item)}"
        )


# ---------------------------------------------------------------------------
# Test 15 — Model receives the exact frame passed to detect()
# ---------------------------------------------------------------------------

def test_frame_passed_to_model_unchanged():
    """
    The frame object given to detect() must be the same object forwarded
    to the underlying model callable.
    """
    frame = object()   # unique sentinel — identity check via `is`

    fake_result = _FakeResult(boxes=[], names=NAMES)
    fake_model  = _FakeModel(fake_result)
    detector = YOLODetector(_model=fake_model)

    detector.detect(frame)

    assert fake_model.last_frame is frame


# ---------------------------------------------------------------------------
# Test 16 — Module import does not load a real YOLO model
# ---------------------------------------------------------------------------

def test_module_import_does_not_require_ultralytics():
    """
    Importing edge.model.yolo_detector must succeed even when ultralytics
    is not on sys.path.  This verifies the lazy-import design.

    We reload the module after temporarily hiding 'ultralytics' from sys.modules.
    """
    saved = sys.modules.pop("ultralytics", None)
    try:
        # Re-importing with ultralytics hidden should not raise.
        import importlib
        import edge.model.yolo_detector  # noqa: F401
        importlib.reload(edge.model.yolo_detector)
    except ImportError as exc:
        if "ultralytics" in str(exc):
            pytest.fail(
                "yolo_detector module imported ultralytics at module level, "
                "which violates the lazy-import requirement."
            )
    finally:
        if saved is not None:
            sys.modules["ultralytics"] = saved


# ---------------------------------------------------------------------------
# Test 17 — Injected model constructor does not import ultralytics
# ---------------------------------------------------------------------------

def test_injected_model_constructor_skips_ultralytics():
    """
    When _model is injected, the constructor must NOT call `ultralytics.YOLO`.
    Verified by instantiating with a fake model and checking no real model load.
    """
    fake_result = _FakeResult(boxes=[], names=NAMES)
    fake_model  = _FakeModel(fake_result)

    # This must not raise even if ultralytics is unavailable.
    saved = sys.modules.pop("ultralytics", None)
    try:
        detector = YOLODetector(_model=fake_model)
        assert detector._model is fake_model
    finally:
        if saved is not None:
            sys.modules["ultralytics"] = saved


# ---------------------------------------------------------------------------
# Additional quality checks
# ---------------------------------------------------------------------------

def test_detect_always_returns_list_type():
    """detect() return type is always list, even when empty."""
    detector = _make_detector([])
    result = detector.detect("frame")
    assert isinstance(result, list)


def test_none_frame_returns_empty_list():
    """detect(None) must return [] gracefully rather than raising."""
    detector = _make_detector([_FakeBox(cls_id=3, conf=0.90)])
    result = detector.detect(None)
    assert result == []


def test_confidence_threshold_property():
    """confidence_threshold property returns the configured value."""
    detector = _make_detector([], threshold=0.42)
    assert abs(detector.confidence_threshold - 0.42) < 1e-9


def test_default_model_path_is_relative():
    """DEFAULT_MODEL_PATH must be a relative/project-based Path, not absolute Colab/OS path."""
    path_str = str(DEFAULT_MODEL_PATH)
    assert "/content/" not in path_str, "Absolute Colab path detected"
    assert "data/model_info/best.pt" in path_str.replace("\\", "/")


def test_default_inference_threshold_value():
    """DEFAULT_INFERENCE_THRESHOLD must be 0.25."""
    assert abs(DEFAULT_INFERENCE_THRESHOLD - 0.25) < 1e-9


def test_mixed_threshold_boundary():
    """
    Three boxes: one below, one exactly at, one above the threshold.
    Exactly two must be returned.
    """
    threshold = 0.30
    boxes = [
        _FakeBox(cls_id=0, conf=0.29),   # excluded
        _FakeBox(cls_id=1, conf=0.30),   # included (boundary)
        _FakeBox(cls_id=2, conf=0.31),   # included
    ]
    detector = _make_detector(boxes, threshold=threshold)
    result = detector.detect("frame")
    assert len(result) == 2
    confs = sorted(d.confidence for d in result)
    assert abs(confs[0] - 0.30) < 1e-9
    assert abs(confs[1] - 0.31) < 1e-9
