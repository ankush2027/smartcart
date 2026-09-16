"""
YOLODetector — lightweight inference adapter for the trained SmartCart YOLO model.

Responsibilities:
    - Load a trained Ultralytics YOLOv8 model from disk.
    - Accept a raw camera frame (NumPy array).
    - Run inference and return a flat list of Detection objects.
    - Apply a configurable inference confidence threshold.

Non-responsibilities:
    - Does NOT access SQLite, billing, or product repository.
    - Does NOT consult ProductResolver or CartSession.
    - Does NOT mutate CartState.
    - Does NOT open or manage a camera.
    - Does NOT perform event correlation or frame buffering.
    - Does NOT implement Firebase, theft detection, or MOG2.

Architecture:
    frame (NumPy array)
        ↓
    YOLODetector.detect(frame)
        ↓
    list[Detection]   ← same Detection from edge.detection.product_resolver
        ↓
    ProductResolver   (separate Task — not wired here)

Confidence layers:
    YOLODetector:    raw inference filter  (DEFAULT_INFERENCE_THRESHOLD = 0.25)
    ProductResolver: business-level gate   (DEFAULT_CONFIDENCE_THRESHOLD  = 0.50)
    These are independent — do not merge them.
"""

from __future__ import annotations

from pathlib import Path

from edge.detection.product_resolver import Detection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Project-relative path to the trained model weights.
#: Place your fine-tuned best.pt here before running real inference.
#: This file is gitignored (*.pt) and must not be committed.
DEFAULT_MODEL_PATH: Path = (
    Path(__file__).parent.parent.parent / "data" / "model_info" / "best.pt"
)

#: Raw inference confidence filter.
#: Detections below this value are dropped before returning to the caller.
#: ProductResolver applies its own independent threshold (0.50) later.
DEFAULT_INFERENCE_THRESHOLD: float = 0.25


# ---------------------------------------------------------------------------
# YOLODetector
# ---------------------------------------------------------------------------

class YOLODetector:
    """
    Thin inference adapter around an Ultralytics YOLOv8 model.

    Parameters
    ----------
    model_path : Path or str, optional
        Path to the ``best.pt`` weights file.
        Defaults to ``data/model_info/best.pt`` relative to the project root.
    confidence_threshold : float, optional
        Minimum confidence for a detection to be included in the result.
        Detections with ``confidence >= confidence_threshold`` are kept.
        Defaults to :data:`DEFAULT_INFERENCE_THRESHOLD` (0.25).
    _model : object, optional
        Injected model object.  When supplied, ``model_path`` is ignored and
        ``ultralytics`` is never imported.  **For testing only.**

    Usage
    -----
    Production::

        detector = YOLODetector()               # loads data/model_info/best.pt
        detections = detector.detect(frame)     # list[Detection]

    Tests (no best.pt required)::

        detector = YOLODetector(_model=fake_model)
        detections = detector.detect(frame)
    """

    def __init__(
        self,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        confidence_threshold: float = DEFAULT_INFERENCE_THRESHOLD,
        _model=None,
    ) -> None:
        self._confidence_threshold = float(confidence_threshold)

        if _model is not None:
            # Injected fake — used in tests; ultralytics is never imported.
            self._model = _model
        else:
            # Lazy import: only pay the ultralytics/torch cost when a real
            # model is actually needed.  This keeps module-level imports fast
            # and avoids breaking the test suite when ultralytics is absent.
            from ultralytics import YOLO  # noqa: PLC0415

            self._model = YOLO(str(model_path))

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def detect(self, frame) -> list[Detection]:
        """
        Run YOLO inference on a single frame.

        Parameters
        ----------
        frame :
            A NumPy array (H × W × 3, BGR) as returned by OpenCV or
            MotionPipeline.  The frame is passed directly to the model.

        Returns
        -------
        list[Detection]
            One :class:`~edge.detection.product_resolver.Detection` per
            bounding box whose confidence is at or above the configured
            threshold.  Returns an empty list when nothing is detected.
            Never returns ``None``.

        Notes
        -----
        - Class names are read from ``result.names`` (embedded in ``best.pt``).
        - Detections are NOT deduplicated — ProductResolver handles that.
        - Bounding-box coordinates are intentionally not included in
          ``Detection``; they are available via ``box.xyxy`` if needed later.
        """
        if frame is None:
            return []

        results = self._model(frame)

        detections: list[Detection] = []

        for result in results:
            names: dict = result.names   # {class_id: class_name_str}

            for box in result.boxes:
                cls_id = int(box.cls[0])
                conf   = float(box.conf[0])

                if conf < self._confidence_threshold:
                    continue

                cls_name = names[cls_id]

                detections.append(
                    Detection(
                        yolo_class=cls_name,
                        confidence=conf,
                    )
                )

        return detections

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    @property
    def confidence_threshold(self) -> float:
        """The currently configured inference confidence threshold."""
        return self._confidence_threshold
