"""
SmartCart YOLO Camera Demo
==========================

Run live YOLO inference on the laptop webcam and visualise the 6 trained
SmartCart product classes:

    0  1 to 3 Noodles Rs 10
    1  1 to 3 Noodles Rs 5
    2  Bru
    3  Coca Cola
    4  Lays
    5  Lifebuoy

Usage
-----
From the project root with the project venv active::

    .venv/bin/python -m demo.yolo_camera_demo

Or directly::

    .venv/bin/python demo/yolo_camera_demo.py

Controls
--------
    q  — quit
    s  — save an annotated snapshot to demo/snapshots/

IMPORTANT
---------
This file is a DEMO only.  It does NOT touch CartSession, billing,
ProductResolver, CartState, the Orchestrator, MOG2, or Firebase.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve project root so the script works when run directly or as a module.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Imports — cv2 and YOLODetector only; nothing else from the pipeline.
# ---------------------------------------------------------------------------
try:
    import cv2
except ImportError:
    sys.exit(
        "[ERROR] OpenCV not found.  Install with:\n"
        "  .venv/bin/pip install opencv-python"
    )

from edge.model.yolo_detector import DEFAULT_MODEL_PATH, YOLODetector

# ---------------------------------------------------------------------------
# Visual design constants
# ---------------------------------------------------------------------------

# One distinct BGR colour per class
_CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "1 to 3 Noodles Rs 10": (0,   200, 255),   # amber
    "1 to 3 Noodles Rs 5":  (0,   140, 255),   # orange
    "Bru":                   (180,  60, 255),   # purple
    "Coca Cola":             (0,    50, 220),   # red
    "Lays":                  (0,   220,   0),   # green
    "Lifebuoy":              (220, 100,   0),   # teal-blue
}
_DEFAULT_COLOR: tuple[int, int, int] = (200, 200, 200)

_FONT                = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE_LABEL    = 0.55
_FONT_SCALE_HUD      = 0.6
_FONT_THICKNESS      = 2
_BOX_THICKNESS       = 2

_HUD_MARGIN          = 14   # px from left edge
_HUD_TOP             = 28   # px from top for first HUD line
_HUD_LINE_HEIGHT     = 26   # px between HUD lines

_SNAPSHOT_DIR        = _PROJECT_ROOT / "demo" / "snapshots"


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

def _color_for(class_name: str) -> tuple[int, int, int]:
    return _CLASS_COLORS.get(class_name, _DEFAULT_COLOR)


def _draw_box(
    frame,
    xyxy: list[float],
    class_name: str,
    confidence: float,
) -> None:
    """Draw one bounding box + label on *frame* (in-place)."""
    x1, y1, x2, y2 = int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])
    color = _color_for(class_name)
    label = f"{class_name}  {confidence:.0%}"

    # Box
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, _BOX_THICKNESS)

    # Label background
    (text_w, text_h), baseline = cv2.getTextSize(
        label, _FONT, _FONT_SCALE_LABEL, _FONT_THICKNESS
    )
    pad = 4
    bg_y1 = max(y1 - text_h - baseline - pad * 2, 0)
    bg_y2 = max(y1, text_h + baseline + pad * 2)
    cv2.rectangle(frame, (x1, bg_y1), (x1 + text_w + pad * 2, bg_y2), color, -1)

    # Label text (white on coloured background)
    cv2.putText(
        frame,
        label,
        (x1 + pad, bg_y2 - baseline - pad),
        _FONT,
        _FONT_SCALE_LABEL,
        (255, 255, 255),
        _FONT_THICKNESS,
        cv2.LINE_AA,
    )


def _draw_hud(frame, detections_this_frame: int, fps: float) -> None:
    """Draw the top-left heads-up display: object count and FPS."""
    lines = [
        f"Objects detected: {detections_this_frame}",
        f"FPS: {fps:.1f}",
        "q=quit  s=save",
    ]
    for i, text in enumerate(lines):
        y = _HUD_TOP + i * _HUD_LINE_HEIGHT
        # Shadow for readability
        cv2.putText(frame, text, (_HUD_MARGIN + 1, y + 1), _FONT,
                    _FONT_SCALE_HUD, (0, 0, 0), _FONT_THICKNESS + 1, cv2.LINE_AA)
        cv2.putText(frame, text, (_HUD_MARGIN, y), _FONT,
                    _FONT_SCALE_HUD, (255, 255, 255), _FONT_THICKNESS, cv2.LINE_AA)


def _save_snapshot(frame) -> None:
    """Save the annotated frame to demo/snapshots/."""
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = _SNAPSHOT_DIR / f"snapshot_{ts}.jpg"
    cv2.imwrite(str(out_path), frame)
    print(f"[DEMO] Snapshot saved -> {out_path}")


# ---------------------------------------------------------------------------
# Raw Ultralytics result access helper
# ---------------------------------------------------------------------------

def _get_boxes_with_coords(results):
    """
    Yield (xyxy, cls_id, class_name, confidence) from raw Ultralytics results.

    YOLODetector.detect() intentionally drops bounding-box coordinates (they
    are not needed by the production pipeline).  For the demo we need them for
    visualisation, so we read result.boxes.xyxy directly from the raw results.
    """
    for result in results:
        names: dict = result.names
        for box in result.boxes:
            cls_id = int(box.cls[0])
            conf   = float(box.conf[0])
            yield box.xyxy[0].tolist(), cls_id, names[cls_id], conf


# ---------------------------------------------------------------------------
# Main demo loop
# ---------------------------------------------------------------------------

def run_demo(
    camera_index: int = 0,
    model_path: Path = DEFAULT_MODEL_PATH,
    confidence_threshold: float = 0.30,
) -> None:
    """
    Open the webcam and run live YOLO inference until the user presses *q*.

    Parameters
    ----------
    camera_index:
        OpenCV camera index (0 = built-in webcam).
    model_path:
        Path to the trained ``best.pt`` weights file.
    confidence_threshold:
        Minimum confidence for a detection to be drawn.
    """
    # --- Model ---
    if not Path(model_path).exists():
        sys.exit(
            f"[ERROR] Model not found at: {model_path}\n"
            "Place best.pt at data/model_info/best.pt and retry."
        )

    print(f"[DEMO] Loading model from: {model_path}")
    try:
        detector = YOLODetector(
            model_path=model_path,
            confidence_threshold=confidence_threshold,
        )
    except Exception as exc:
        sys.exit(f"[ERROR] Failed to load YOLO model: {exc}")

    print("[DEMO] Model loaded successfully.")

    # --- Camera ---
    print(f"[DEMO] Opening camera index {camera_index} ...")
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        sys.exit(
            f"[ERROR] Cannot open camera (index={camera_index}).\n"
            "Check that no other application is using the webcam."
        )
    print("[DEMO] Camera opened.  Press  q  to quit,  s  to save a snapshot.")
    print("[DEMO] Show any SmartCart product to the camera:")
    print("         - Noodles Rs 10  - Noodles Rs 5")
    print("         - Bru            - Coca Cola")
    print("         - Lays           - Lifebuoy")

    # --- FPS tracking ---
    prev_time   = time.perf_counter()
    fps         = 0.0
    frame_count = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[WARN] Frame capture failed -- retrying ...")
                continue

            # Run raw inference so we can access xyxy for drawing.
            raw_results = detector._model(frame)

            # Also run detect() for the public detection count.
            detections = detector.detect(frame)
            n_detected = len(detections)

            # Draw boxes for detections that pass the threshold.
            for xyxy, _cls_id, class_name, confidence in _get_boxes_with_coords(raw_results):
                if confidence < confidence_threshold:
                    continue
                _draw_box(frame, xyxy, class_name, confidence)

            # FPS
            now       = time.perf_counter()
            elapsed   = now - prev_time
            prev_time = now
            if elapsed > 0:
                fps = 1.0 / elapsed

            _draw_hud(frame, n_detected, fps)

            cv2.imshow("SmartCart YOLO Demo  |  q=quit  s=save", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("[DEMO] Quit requested.")
                break
            if key == ord("s"):
                _save_snapshot(frame)

            frame_count += 1

    finally:
        cap.release()
        cv2.destroyAllWindows()
        print(f"[DEMO] Session ended after {frame_count} frames.")


# ---------------------------------------------------------------------------
# Entry point -- only executes when run directly or as a module; never on import.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_demo()
