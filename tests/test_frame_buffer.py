"""
Unit tests for FrameBuffer.

Covers:
    - Adding frames with automatic timestamping
    - Adding frames with explicit timestamps
    - Maximum-frame eviction (oldest dropped when full)
    - Closest-frame lookup (best match selected)
    - max-age rejection (too-far frames return None)
    - get_recent_frames (all and bounded counts)
    - clear()
    - Empty-buffer behaviour

No camera, no OpenCV, no hardware.
All frames are simple synthetic objects (integers / strings).
"""

from datetime import datetime, timedelta

from edge.detection.frame_buffer import FrameBuffer, DEFAULT_MAX_FRAMES, DEFAULT_MAX_AGE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_seconds=0.0, base=None):
    """Return a deterministic datetime offset from a base time."""
    if base is None:
        base = datetime(2026, 1, 1, 12, 0, 0)
    return base + timedelta(seconds=offset_seconds)


def _buf(max_frames=None, max_age=None):
    """Convenience factory that uses project defaults when not specified."""
    kwargs = {}
    if max_frames is not None:
        kwargs["max_frames"] = max_frames
    if max_age is not None:
        kwargs["max_age"] = max_age
    return FrameBuffer(**kwargs)


# ---------------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------------

def test_default_construction():
    """Buffer starts empty and respects DEFAULT constants."""
    buf = FrameBuffer()
    assert len(buf) == 0
    assert buf.max_frames == DEFAULT_MAX_FRAMES
    assert buf.max_age == DEFAULT_MAX_AGE


def test_custom_construction():
    """Custom max_frames and max_age are stored correctly."""
    buf = FrameBuffer(max_frames=10, max_age=5.0)
    assert buf.max_frames == 10
    assert buf.max_age == 5.0


# ---------------------------------------------------------------------------
# Adding frames — automatic timestamp
# ---------------------------------------------------------------------------

def test_add_increments_length():
    """Each add() increases the buffer length by 1."""
    buf = _buf()
    assert len(buf) == 0
    buf.add("frame-a")
    assert len(buf) == 1
    buf.add("frame-b")
    assert len(buf) == 2


def test_add_auto_timestamp_is_datetime():
    """When no timestamp is supplied, the stored timestamp is a datetime."""
    buf = _buf()
    buf.add("frame-x")
    ts, frame = buf.get_recent_frames(count=1)[0]
    assert isinstance(ts, datetime)
    assert frame == "frame-x"


# ---------------------------------------------------------------------------
# Adding frames — explicit timestamp
# ---------------------------------------------------------------------------

def test_add_explicit_timestamp_stored():
    """Explicit timestamp is preserved exactly."""
    buf = _buf()
    t = _ts(10.0)
    buf.add("explicit-frame", timestamp=t)
    ts, frame = buf.get_recent_frames()[0]
    assert ts == t
    assert frame == "explicit-frame"


def test_multiple_explicit_timestamps_ordered():
    """Frames are stored in insertion order."""
    buf = _buf()
    t0 = _ts(0.0)
    t1 = _ts(1.0)
    t2 = _ts(2.0)
    buf.add("f0", timestamp=t0)
    buf.add("f1", timestamp=t1)
    buf.add("f2", timestamp=t2)

    frames = buf.get_recent_frames()
    assert [f for _, f in frames] == ["f0", "f1", "f2"]


# ---------------------------------------------------------------------------
# Maximum-frame eviction
# ---------------------------------------------------------------------------

def test_max_frames_evicts_oldest():
    """When the buffer is full, adding a new frame evicts the oldest."""
    buf = _buf(max_frames=3)
    t = datetime(2026, 1, 1, 12, 0, 0)

    for i in range(3):
        buf.add(f"frame-{i}", timestamp=t + timedelta(seconds=i))

    assert len(buf) == 3

    # Adding a 4th should evict "frame-0"
    buf.add("frame-3", timestamp=t + timedelta(seconds=3))
    assert len(buf) == 3

    frames = [f for _, f in buf.get_recent_frames()]
    assert "frame-0" not in frames
    assert "frame-1" in frames
    assert "frame-2" in frames
    assert "frame-3" in frames


def test_max_frames_never_exceeded():
    """Buffer length never exceeds max_frames after many insertions."""
    max_frames = 5
    buf = _buf(max_frames=max_frames)
    t = datetime(2026, 1, 1, 12, 0, 0)

    for i in range(20):
        buf.add(f"f{i}", timestamp=t + timedelta(seconds=i))
        assert len(buf) <= max_frames


def test_max_frames_one():
    """A buffer of size 1 always contains only the most recent frame."""
    buf = _buf(max_frames=1)
    buf.add("old")
    buf.add("new")
    assert len(buf) == 1
    _, frame = buf.get_recent_frames()[0]
    assert frame == "new"


# ---------------------------------------------------------------------------
# Closest-frame lookup — happy paths
# ---------------------------------------------------------------------------

def test_get_closest_frame_exact_match():
    """Frame stored at exactly the target timestamp is returned."""
    buf = _buf()
    t = _ts(5.0)
    buf.add("exact", timestamp=t)
    entry = buf.get_closest_frame(t)
    assert entry is not None
    ts, frame = entry
    assert ts == t
    assert frame == "exact"


def test_get_closest_frame_selects_nearest():
    """The frame nearest (not most recent) to the target is selected."""
    buf = _buf(max_age=60.0)
    base = _ts(0.0)

    buf.add("early",  timestamp=base + timedelta(seconds=1.0))   # 1 s from t=3
    buf.add("middle", timestamp=base + timedelta(seconds=3.0))   # 0 s from t=3 ← best
    buf.add("late",   timestamp=base + timedelta(seconds=9.0))   # 6 s from t=3

    target = base + timedelta(seconds=3.0)
    entry = buf.get_closest_frame(target)
    assert entry is not None
    _, frame = entry
    assert frame == "middle"


def test_get_closest_frame_does_not_always_pick_latest():
    """
    A buffered frame that is older but closer wins over the most recent
    frame that is further away.

    Regression guard for the requirement:
        "The most recent frame is NOT incorrectly selected when another
        buffered frame is closer to the movement timestamp."
    """
    buf = _buf(max_age=60.0)
    base = datetime(2026, 1, 1, 12, 0, 0)

    # Frame captured at T+0 s
    buf.add("close-frame",  timestamp=base + timedelta(seconds=0.0))
    # Frame captured at T+5 s (most recent but far from target)
    buf.add("recent-frame", timestamp=base + timedelta(seconds=5.0))

    # Target is T+0.1 s — close-frame is 0.1 s away, recent-frame is 4.9 s away
    target = base + timedelta(seconds=0.1)
    entry = buf.get_closest_frame(target)
    assert entry is not None
    _, frame = entry
    assert frame == "close-frame", (
        f"Expected 'close-frame' (closer to target), got '{frame}'"
    )


def test_get_closest_frame_single_entry():
    """A buffer with one frame returns that frame when within max_age."""
    buf = _buf(max_age=2.0)
    t = _ts(0.0)
    buf.add("only", timestamp=t)
    entry = buf.get_closest_frame(t + timedelta(seconds=1.0))
    assert entry is not None
    _, frame = entry
    assert frame == "only"


# ---------------------------------------------------------------------------
# max-age rejection
# ---------------------------------------------------------------------------

def test_get_closest_frame_returns_none_beyond_max_age():
    """
    When the closest frame is farther than max_age seconds from the
    target, get_closest_frame() returns None.
    """
    buf = _buf(max_age=2.0)
    t = _ts(0.0)
    buf.add("stale", timestamp=t)

    # Target is 3 s later — beyond the 2 s max_age
    entry = buf.get_closest_frame(t + timedelta(seconds=3.0))
    assert entry is None


def test_get_closest_frame_exactly_at_max_age_returns_frame():
    """
    A frame at exactly max_age distance is within the limit (> not >=).
    """
    max_age = 2.0
    buf = _buf(max_age=max_age)
    t = _ts(0.0)
    buf.add("boundary", timestamp=t)

    # Exactly at max_age — FrameBuffer uses `> max_age` to reject
    entry = buf.get_closest_frame(t + timedelta(seconds=max_age))
    assert entry is not None
    _, frame = entry
    assert frame == "boundary"


def test_get_closest_frame_just_beyond_max_age_returns_none():
    """A frame just over max_age is rejected."""
    max_age = 2.0
    buf = _buf(max_age=max_age)
    t = _ts(0.0)
    buf.add("too-old", timestamp=t)

    # Fractionally beyond max_age
    entry = buf.get_closest_frame(t + timedelta(seconds=max_age + 0.001))
    assert entry is None


# ---------------------------------------------------------------------------
# Empty-buffer behaviour
# ---------------------------------------------------------------------------

def test_get_closest_frame_empty_buffer_returns_none():
    """get_closest_frame() on an empty buffer returns None immediately."""
    buf = _buf()
    assert buf.get_closest_frame(datetime.now()) is None


def test_get_recent_frames_empty_buffer():
    """get_recent_frames() on an empty buffer returns an empty list."""
    buf = _buf()
    assert buf.get_recent_frames() == []
    assert buf.get_recent_frames(count=5) == []


# ---------------------------------------------------------------------------
# get_recent_frames
# ---------------------------------------------------------------------------

def test_get_recent_frames_all():
    """With no count, all frames are returned in insertion order."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        buf.add(f"f{i}", timestamp=t + timedelta(seconds=i))

    frames = buf.get_recent_frames()
    assert len(frames) == 5
    assert [f for _, f in frames] == ["f0", "f1", "f2", "f3", "f4"]


def test_get_recent_frames_bounded():
    """count=N returns the N most-recent frames."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        buf.add(f"f{i}", timestamp=t + timedelta(seconds=i))

    frames = buf.get_recent_frames(count=3)
    assert len(frames) == 3
    assert [f for _, f in frames] == ["f2", "f3", "f4"]


def test_get_recent_frames_count_larger_than_buffer():
    """count larger than buffer size returns all available frames."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    buf.add("only", timestamp=t)

    frames = buf.get_recent_frames(count=100)
    assert len(frames) == 1
    assert frames[0][1] == "only"


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------

def test_clear_empties_buffer():
    """clear() removes all frames."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        buf.add(f"f{i}", timestamp=t + timedelta(seconds=i))

    assert len(buf) == 5
    buf.clear()
    assert len(buf) == 0


def test_clear_then_add():
    """After clear(), the buffer can accept new frames normally."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    buf.add("old", timestamp=t)
    buf.clear()
    buf.add("new", timestamp=t + timedelta(seconds=1))

    assert len(buf) == 1
    _, frame = buf.get_recent_frames()[0]
    assert frame == "new"


def test_get_closest_frame_after_clear():
    """get_closest_frame() returns None after clear()."""
    buf = _buf()
    t = datetime(2026, 1, 1, 12, 0, 0)
    buf.add("frame", timestamp=t)
    buf.clear()
    assert buf.get_closest_frame(t) is None
