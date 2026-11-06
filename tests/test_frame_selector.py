"""Pure frame quality/temporal selection, no product or model knowledge."""
from datetime import datetime, timedelta
import cv2
import numpy as np
import pytest
from edge.detection.frame_buffer import FrameBuffer
from edge.detection.frame_selector import FrameSelector, FrameSelectionError, sharpness

T = datetime(2026, 1, 1)


def test_inclusive_window_excludes_outside_frames():
    buffer = FrameBuffer()
    for second, score in [(0.299, 100), (.3, 1), (.5, 5), (.8, 2), (.801, 200)]:
        buffer.add(score, T + timedelta(seconds=second))
    seen = []
    def score(frame):
        seen.append(frame)
        return frame
    result = FrameSelector(scorer=score).select(buffer, T)
    assert seen == [1, 5, 2]
    assert result['candidate_count'] == 3
    assert result['quality_score'] == 5 and result['frame'] == 5
    assert result['frame_delta'] == .5
    assert result['window_start'] == T + timedelta(seconds=.3)
    assert result['window_end'] == T + timedelta(seconds=.8)
    assert len(result['candidate_scores']) == 3


def test_real_sharpness_selects_sharp_over_blur_without_mutation():
    image = np.zeros((96, 96, 3), dtype=np.uint8)
    image[24:72, 24:72] = 255
    blur = cv2.GaussianBlur(image, (15, 15), 5)
    original = image.copy()
    buffer = FrameBuffer()
    buffer.add(blur, T + timedelta(seconds=.4))
    buffer.add(image, T + timedelta(seconds=.6))
    result = FrameSelector().select(buffer, T)
    assert result['frame'] is image and result['quality_score'] > sharpness(blur)
    assert np.array_equal(image, original)


def test_tie_prefers_earliest_timestamp_then_buffer_order():
    buffer = FrameBuffer()
    buffer.add('late', T + timedelta(seconds=.7))
    buffer.add('early-first', T + timedelta(seconds=.3))
    buffer.add('early-second', T + timedelta(seconds=.3))
    assert FrameSelector(scorer=lambda _: 1).select(buffer, T)['frame'] == 'early-first'


def test_one_candidate_is_valid():
    buffer = FrameBuffer(); buffer.add('one', T + timedelta(seconds=.4))
    result = FrameSelector(scorer=lambda _: 0).select(buffer, T)
    assert result['candidate_count'] == 1 and result['quality_score'] == 0


def test_zero_candidates_is_explicit():
    with pytest.raises(FrameSelectionError, match='No frames'):
        FrameSelector().select(FrameBuffer(), T)


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -1])
def test_invalid_score_rejected(score):
    buffer = FrameBuffer(); buffer.add('frame', T + timedelta(seconds=.4))
    with pytest.raises(FrameSelectionError, match='Invalid'):
        FrameSelector(scorer=lambda _: score).select(buffer, T)


@pytest.mark.parametrize('start,end', [(-.1, .8), (.8, .3), (0, float('inf'))])
def test_invalid_window_rejected(start, end):
    with pytest.raises(ValueError):
        FrameSelector(window_start=start, window_end=end)


def test_large_frames_scored_at_bounded_resolution(monkeypatch):
    dimensions = []
    original = cv2.Laplacian
    def laplacian(image, depth):
        dimensions.append(image.shape)
        return original(image, depth)
    monkeypatch.setattr(cv2, 'Laplacian', laplacian)
    assert sharpness(np.zeros((720, 1280, 3), dtype=np.uint8)) == 0
    assert dimensions == [(180, 320)]
