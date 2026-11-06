"""Product-agnostic temporal candidate selection; no detection or billing."""
import math

DEFAULT_WINDOW_START = 0.3
DEFAULT_WINDOW_END = 0.8
QUALITY_MAX_DIMENSION = 320


class FrameSelectionError(RuntimeError):
    pass


def sharpness(frame):
    """Variance of grayscale Laplacian; bounded working image, no source edits."""
    import cv2
    if frame is None or frame.size == 0:
        raise FrameSelectionError('Cannot score an empty frame')
    height, width = frame.shape[:2]
    scale = min(1.0, QUALITY_MAX_DIMENSION / max(height, width))
    image = frame
    if scale < 1:
        image = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class FrameSelector:
    """Inclusive time window; greatest sharpness, then earliest timestamp.

    One candidate is sufficient. Equal timestamp/score ties keep buffer order.
    Sharpness is not a product-visibility guarantee. Never runs YOLO.
    """
    def __init__(self, *, window_start=DEFAULT_WINDOW_START,
                 window_end=DEFAULT_WINDOW_END, scorer=sharpness):
        if (not math.isfinite(window_start) or not math.isfinite(window_end)
                or window_start < 0 or window_end < window_start):
            raise ValueError('Candidate window requires finite 0 <= start <= end')
        self.window_start, self.window_end, self.scorer = window_start, window_end, scorer

    def bounds(self, movement_timestamp):
        from datetime import timedelta
        return (movement_timestamp + timedelta(seconds=self.window_start),
                movement_timestamp + timedelta(seconds=self.window_end))

    def select(self, buffer, movement_timestamp):
        start, end = self.bounds(movement_timestamp)
        candidates = [(ts, frame) for ts, frame in buffer.get_recent_frames() if start <= ts <= end]
        if not candidates:
            raise FrameSelectionError('No frames in completed candidate window; event not dispatched')
        ranked = []
        for ts, frame in candidates:
            score = float(self.scorer(frame))
            if not math.isfinite(score) or score < 0:
                raise FrameSelectionError('Invalid candidate quality score; event not dispatched')
            ranked.append((ts, frame, score))
        selected_ts, selected, quality = min(ranked, key=lambda entry: (-entry[2], entry[0]))
        return {'frame': selected, 'frame_timestamp': selected_ts,
                'window_start': start, 'window_end': end,
                'frame_delta': (selected_ts - movement_timestamp).total_seconds(),
                'quality_score': quality, 'candidate_count': len(ranked),
                'candidate_scores': [(ts, score) for ts, _, score in ranked]}
