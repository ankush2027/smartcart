"""Terminal parser regressions, with no camera, session, database or network."""
from collections import deque
from types import SimpleNamespace

import pytest
from edge.runtime import weight_source as module


@pytest.fixture
def feed(monkeypatch):
    chunks = deque()
    stdin = SimpleNamespace(fileno=lambda: 42)
    monkeypatch.setattr(module.sys, 'stdin', stdin)
    def select(readers, writers, errors, timeout):
        assert readers == [stdin] and timeout == 0
        return ([stdin] if chunks else [], [], [])
    def read(fd, size):
        assert fd == 42 and size == 4096
        return chunks.popleft()
    monkeypatch.setattr(module.select, 'select', select)
    monkeypatch.setattr(module.os, 'read', read)
    return module.StdinSimulatedWeightSource(500), chunks


@pytest.mark.parametrize('line,delta', [(b'+50\n', 50), (b'-50\n', -50)])
def test_signed_delta(feed, line, delta):
    source, chunks = feed
    chunks.append(line)
    event = source.poll()
    assert event.weight_change == delta
    assert event.event_type == ('ADD' if delta > 0 else 'REMOVE')
    assert source.baseline == source.sensor.read_weight() == 500 + delta


@pytest.mark.parametrize('line', [b'\n', b' \t\r\n'])
def test_blank_never_parses_or_injects_delta(feed, monkeypatch, line):
    source, chunks = feed
    chunks.append(line)
    def forbidden(*args):
        pytest.fail('Blank record must not reach float conversion or delta injection')
    monkeypatch.setattr(module, 'float', forbidden, raising=False)
    monkeypatch.setattr(source, 'inject_delta', forbidden)
    assert source.poll() is None
    assert source.baseline == source.sensor.read_weight() == 500


def test_buffered_valid_line_after_blank(feed):
    source, chunks = feed
    chunks.append(b'\n+50\n-50\n')
    assert source.poll() is None
    assert source.baseline == 500
    assert source.poll().weight_change == 50
    assert source.poll().weight_change == -50
    assert source.baseline == source.sensor.read_weight() == 500


def test_partial_line_and_no_data_are_nonblocking(feed):
    source, chunks = feed
    assert source.poll() is None
    chunks.append(b'+5')
    assert source.poll() is None
    assert source.poll() is None  # No additional bytes; retain partial input.
    assert source.baseline == source.sensor.read_weight() == 500
    chunks.append(b'0\n')
    assert source.poll().weight_change == 50


@pytest.mark.parametrize('prefix', [b'', b'+5', b'\n'])
def test_actual_eof_still_raises(feed, prefix):
    source, chunks = feed
    if prefix:
        chunks.append(prefix)
        assert source.poll() is None
    chunks.append(b'')
    with pytest.raises(EOFError, match='Simulation input closed'):
        source.poll()
    assert source.baseline == source.sensor.read_weight() == 500


def test_overlong_buffer_still_raises(feed):
    source, chunks = feed
    chunks.extend([b' ' * 4096, b' ' * 4096, b' \n'])
    assert source.poll() is None
    assert source.poll() is None
    with pytest.raises(ValueError, match='Simulation input line too long'):
        source.poll()
    assert source.baseline == source.sensor.read_weight() == 500


@pytest.mark.parametrize('line', [b'abc\n', b'+50 g\n'])
def test_nonempty_invalid_text_still_raises(feed, line):
    source, chunks = feed
    chunks.append(line)
    with pytest.raises(ValueError, match='could not convert string to float'):
        source.poll()
    assert source.baseline == source.sensor.read_weight() == 500
