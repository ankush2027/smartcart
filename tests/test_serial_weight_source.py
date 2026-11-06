"""Offline tests for HX711 JSON Lines parsing and physical-source selection."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import time

import pytest

from edge.runtime.weight_source import (
    SerialWeightSource,
    StdinSimulatedWeightSource,
    create_weight_source,
)
from edge.runtime.run_cart import build_parser, parse_args as parse_runtime_args, select_weight_source
from edge.detection.event import DetectionEvent
from edge.orchestrator import EventOrchestrator


class FakeSerial:
    def __init__(self, lines=(), *, read_error=None, max_chunk=None, tare_uptime=900):
        self.data = bytearray(b''.join(line if isinstance(line, bytes) else line.encode() for line in lines))
        if read_error is not None and not self.data:
            self.data.extend(b'x')
        self.read_error = read_error
        self.max_chunk = max_chunk
        self.tare_uptime = tare_uptime
        self.writes = []
        self.closed = False
        self.is_open = True
        self.reset = 0

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, size):
        if not self.is_open:
            raise ValueError('I/O operation on closed serial port')
        if self.read_error is not None:
            error, self.read_error = self.read_error, None
            raise error
        chunk_size = min(size, self.max_chunk) if self.max_chunk else size
        chunk = bytes(self.data[:chunk_size])
        del self.data[:chunk_size]
        return chunk

    def write(self, value):
        if not self.is_open:
            raise ValueError('I/O operation on closed serial port')
        self.writes.append(value)
        if value.startswith(b'READY? '):
            request_id = int(value[len(b'READY? '):].strip())
            response = {
                'type': 'status', 'status': 'ready', 'protocol_version': 1,
                'sequence': 4, 'uptime_ms': 2000, 'request_id': request_id,
            }
            self.data.extend(json.dumps(response).encode() + b'\n')
        elif value == b'TARE\n':
            response = {
                'type': 'tare', 'ok': True, 'protocol_version': 1,
                'reason': 'ok', 'sequence': 5, 'uptime_ms': self.tare_uptime,
            }
            self.data.extend(json.dumps(response).encode() + b'\n')
        return len(value)

    def flush(self):
        pass

    def reset_input_buffer(self):
        # Lines represent records arriving after the fake port is opened.
        self.reset += 1

    def close(self):
        self.closed = True
        self.is_open = False


def ready_record(*, request_id=None, version=1):
    value = {'type': 'status', 'status': 'ready', 'protocol_version': version,
             'sequence': 0, 'uptime_ms': 0}
    if request_id is not None:
        value['request_id'] = request_id
    return json.dumps(value).encode() + b'\n'


def error_record(code, *, sequence=1, uptime=1000, version=1):
    return json.dumps({
        'type': 'error', 'code': code, 'protocol_version': version,
        'sequence': sequence, 'uptime_ms': uptime,
    }).encode() + b'\n'


@pytest.fixture(autouse=True)
def skip_production_startup_delay_for_unrelated_unit_tests(monkeypatch):
    monkeypatch.setattr(SerialWeightSource, 'READY_STARTUP_GRACE', 0.0)


def record(grams=100.0, *, sequence=1, uptime=1000, stable=True, **overrides):
    value = {
        'type': 'weight', 'protocol_version': 1,
        'grams': grams, 'raw': 123456, 'stable': stable,
        'sequence': sequence, 'uptime_ms': uptime, 'calibrated': True,
    }
    value.update(overrides)
    return json.dumps(value).encode() + b'\n'


def assert_handshake_then_tare(device):
    assert len(device.writes) == 2
    assert device.writes[0].startswith(b'READY? ')
    assert device.writes[0].endswith(b'\n')
    assert device.writes[1] == b'TARE\n'


def test_sketch_declares_and_emits_the_host_protocol_v1_nonce_contract():
    sketch = (Path(__file__).parents[1] / 'firmware/smartcart_hx711/smartcart_hx711.ino').read_text()
    assert 'constexpr uint8_t PROTOCOL_VERSION = 1;' in sketch
    assert 'Serial.begin(115200);' in sketch
    setup = sketch.split('void setup()', 1)[1].split('void loop()', 1)[0]
    assert setup.index('scale.begin(HX711_DOUT_PIN, HX711_SCK_PIN);') < setup.index('emitReady();')
    assert 'strncmp(command, "READY? ", 7) == 0' in sketch
    parser = sketch.split('void handleCommand(', 1)[1].split('void readCommands()', 1)[0]
    assert "const char *digits = command + 7;" in parser
    assert "*digits < '0' || *digits > '9'" in parser
    assert '(0xFFFFFFFFUL - digit) / 10UL' in parser
    assert 'requestId = requestId * 10UL + digit;' in parser
    assert 'emitReady(true, requestId);' in sketch
    assert 'Serial.print(F(",\\\"request_id\\\":"));' in sketch
    reader = sketch.split('void readCommands()', 1)[1].split('bool isStable(', 1)[0]
    assert "if (value == '\\r') continue;" in reader
    assert "if (value == '\\n')" in reader


def test_ready_request_is_retried_with_same_uint32_id_if_initial_command_is_lost(monkeypatch):
    class DropsFirstReadySerial(FakeSerial):
        def __init__(self):
            super().__init__([ready_record(), error_record('tare_required')])
            self.ready_attempts = 0

        def write(self, value):
            if value.startswith(b'READY? '):
                self.ready_attempts += 1
                if self.ready_attempts == 1:
                    self.writes.append(value)  # Simulate Mega reset/bootloader dropping first write.
                    return len(value)
            return super().write(value)

    monkeypatch.setattr('edge.runtime.weight_source.secrets.randbits', lambda bits: 0xFFFFFFFF)
    device = DropsFirstReadySerial()
    source = SerialWeightSource(
        '/dev/fake', serial_factory=lambda **kwargs: device,
    )
    source.tare(ready_timeout=2.0)

    ready_writes = [value for value in device.writes if value.startswith(b'READY? ')]
    assert len(ready_writes) == 2
    assert ready_writes[0] == ready_writes[1]
    assert ready_writes[0] == b'READY? 4294967295\n'
    assert ready_writes[0].endswith(b'\n')
    assert device.writes[-1] == b'TARE\n'
    assert source._ready is True
    assert source._sensor_ready is True


def test_delayed_firmware_startup_is_drained_during_grace_and_matching_reply_follows_it():
    class DelayedStartupSerial(FakeSerial):
        def __init__(self):
            super().__init__()
            self.opened_at = time.monotonic()
            self.boot_at = self.opened_at + 0.02
            self.startup_emitted = False
            self.first_ready_at = None
            self.source = None

        @property
        def in_waiting(self):
            if not self.startup_emitted and time.monotonic() >= self.boot_at:
                self.startup_emitted = True
                self.data.extend(ready_record())
                self.data.extend(error_record('tare_required', sequence=2, uptime=600))
            return len(self.data)

        def write(self, value):
            if value.startswith(b'READY? '):
                self.first_ready_at = time.monotonic()
                assert self.startup_emitted
                assert self.source._sensor_ready  # startup tare_required was parsed during grace
            return super().write(value)

    device = DelayedStartupSerial()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.READY_STARTUP_GRACE = 0.06
    device.source = source
    source.tare(ready_timeout=0.5)

    assert device.first_ready_at - device.opened_at >= source.READY_STARTUP_GRACE
    assert_handshake_then_tare(device)
    assert source._ready is True
    assert source._sensor_ready is True
    source.close()


def source_with(lines, *, clock=None, mono=None, factory=None, max_chunk=None,
                pre_tare=False, tare_uptime=900, **kwargs):
    initial_lines = [ready_record(), error_record('tare_required')] if pre_tare else [ready_record()]
    serial_device = FakeSerial([*initial_lines, *([] if pre_tare else lines)],
                               max_chunk=max_chunk, tare_uptime=tare_uptime)
    made = []
    def make(**options):
        made.append(options)
        return factory() if factory else serial_device
    source = SerialWeightSource(
        '/dev/fake', clock=clock or (lambda: datetime(2026, 1, 1)),
        monotonic=mono or (lambda: 10.0), serial_factory=make, **kwargs,
    )
    if pre_tare:
        source.tare()
        serial_device.data.extend(b''.join(lines))
    return source, serial_device, made


def test_valid_stable_samples_emit_existing_weight_event_with_signed_delta_and_timestamp():
    wall = datetime(2026, 1, 1)
    source, _, made = source_with([record(100, sequence=1, uptime=1000),
                                   record(115.5, sequence=2, uptime=1100)],
                                  clock=lambda: wall, mono=lambda: 10.0, pre_tare=True)
    assert source.poll() is None  # first stable sample establishes baseline
    event = source.poll()
    assert event.event_type == 'ADD'
    assert event.previous_weight == 100
    assert event.current_weight == 115.5
    assert event.weight_change == pytest.approx(15.5)
    assert event.timestamp == wall + timedelta(seconds=0.2)
    assert made[0] == {'port': '/dev/fake', 'baudrate': 115200, 'timeout': 0.1}
    source.close()


def test_negative_impossible_missing_and_invalid_records_are_ignored():
    lines = [b'{broken\n', b'{"type":"weight"}\n', record(-1, sequence=1),
             record(20_001, sequence=2), record(100, sequence=3)]
    source, _, _ = source_with(lines, pre_tare=True)
    for _ in lines:
        assert source.poll() is None
    assert source.latest_reading.grams == 100
    assert source.poll() is None  # no new bytes; never fabricates an event
    source.close()


def test_boot_reset_garbage_is_bounded_and_valid_json_readiness_recovers():
    source, device, _ = source_with([
        b'ms":8005}\n',
        b'RAW: -54195\n',
        ready_record(),
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n',
    ])
    source.tare()
    assert_handshake_then_tare(device)
    assert source._ready is True
    assert source._needs_tare is False
    source.close()


def test_jsonl_parser_retains_one_byte_reads_until_newline():
    source, device, _ = source_with([
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\r\n',
    ], max_chunk=1)
    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    source.close()


def test_incomplete_json_never_establishes_readiness_or_sends_tare():
    device = FakeSerial([
        b'{"type":"status","status":"ready","protocol_version":1,"sequence":0,"uptime_ms":1',
    ], max_chunk=3)
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    assert source._connect()
    while device.in_waiting:
        assert source._next_line() is None
    assert source._buffer.startswith(b'{"type":"status"')
    assert source._ready is False
    assert source._sensor_ready is False
    assert device.writes == []
    source.close()


@pytest.mark.parametrize('invalid_readiness', [
    b'{"type":"status","status":"ready","protocol_version":1}\n',
    b'{"type":"error","code":"tare_required","protocol_version":1}\n',
])
def test_readiness_requires_complete_firmware_counters(invalid_readiness):
    device = FakeSerial([invalid_readiness])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    assert source._connect()
    assert source._next_line() == invalid_readiness.rstrip(b'\n')
    source._parse_weight(invalid_readiness.rstrip(b'\n'))
    assert source._ready is False
    assert source._sensor_ready is False
    assert device.writes == []
    source.close()


@pytest.mark.parametrize('version', [2, '1', True])
def test_startup_record_with_wrong_protocol_version_is_not_readiness(version):
    device = FakeSerial([ready_record(version=version)])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    assert source._connect()
    source._parse_weight(source._next_line())
    assert source._ready is False
    assert source._sensor_ready is False
    assert device.writes == []
    source.close()


def test_valid_startup_record_is_parsed_but_nonce_response_completes_handshake():
    device = FakeSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    assert source._connect()
    assert source._parse_weight(source._next_line()) is None
    assert source._ready is False  # valid boot heartbeat is not the request response
    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    source.close()


def test_wrong_or_missing_request_id_never_sends_tare():
    class WrongNonceSerial(FakeSerial):
        def write(self, value):
            self.writes.append(value)
            if value.startswith(b'READY? '):
                request_id = int(value[len(b'READY? '):].strip()) ^ 1
                self.data.extend(ready_record(request_id=request_id))
            return len(value)

    device = WrongNonceSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='no matching versioned READY response') as error:
        source.tare(ready_timeout=0.01)
    assert 'heartbeats_without_request_id=1' in str(error.value)
    assert 'mismatched_request_ids=1' in str(error.value)
    assert 'tare_required_records=1' in str(error.value)
    assert len(device.writes) == 1 and device.writes[0].startswith(b'READY? ')
    assert b'TARE\n' not in device.writes
    source.close()


def test_repeated_tare_required_heartbeats_wait_for_matching_response(caplog):
    device = FakeSerial([error_record('tare_required', sequence=i, uptime=i * 1000)
                         for i in range(1, 5)])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    assert source._readiness_tare_required_records == 4
    assert source._readiness_status_records == 1
    assert source._readiness_mismatched_ids == 0
    assert caplog.text.count('HX711 firmware reports sensor state: tare_required') == 1
    source.close()


def test_delayed_nonce_response_completes_handshake_without_losing_sensor_status():
    class DelayedNonceSerial(FakeSerial):
        def __init__(self, lines):
            super().__init__(lines)
            self.pending_request_id = None
            self.empty_polls = 0

        def write(self, value):
            self.writes.append(value)
            if value.startswith(b'READY? '):
                self.pending_request_id = int(value[len(b'READY? '):].strip())
            elif value == b'TARE\n':
                response = {
                    'type': 'tare', 'ok': True, 'protocol_version': 1,
                    'reason': 'ok', 'sequence': 5, 'uptime_ms': 900,
                }
                self.data.extend(json.dumps(response).encode() + b'\n')
            return len(value)

        @property
        def in_waiting(self):
            if not self.data and self.pending_request_id is not None:
                self.empty_polls += 1
                if self.empty_polls == 3:
                    self.data.extend(ready_record(request_id=self.pending_request_id))
            return len(self.data)

    device = DelayedNonceSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=0.2)
    assert device.empty_polls >= 3
    assert_handshake_then_tare(device)
    source.close()


def test_truncated_startup_suffix_is_discarded_then_complete_readiness_is_accepted():
    device = FakeSerial([
        b'ms":8005}\n',
        ready_record(),
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n',
    ])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    source.close()


def test_malformed_stream_recovery_is_bounded_and_warning_rate_limited(caplog):
    garbage = [b'RAW: -54195\n'] * (SerialWeightSource.MAX_CONSECUTIVE_MALFORMED_RECORDS + 20)
    source, device, _ = source_with(garbage)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        with pytest.raises(RuntimeError, match='consecutive invalid HX711 serial records'):
            source.tare()
    assert len(device.writes) == 1 and device.writes[0].startswith(b'READY? ')
    assert b'TARE\n' not in device.writes
    assert caplog.text.count('Invalid HX711 serial record') <= 2
    assert source._serial is None
    source.close()


@pytest.mark.parametrize('record_value', [
    {'type': 'weight', 'grams': True, 'raw': 1, 'stable': True, 'sequence': 1, 'uptime_ms': 1, 'calibrated': True},
    {'type': 'weight', 'grams': 50, 'raw': 1, 'stable': 1, 'sequence': 1, 'uptime_ms': 1, 'calibrated': True},
    {'type': 'weight', 'grams': 50, 'raw': 1, 'stable': True, 'sequence': True, 'uptime_ms': 1, 'calibrated': True},
    {'type': 'weight', 'grams': 50, 'raw': 1, 'stable': True, 'sequence': 1, 'uptime_ms': 1, 'calibrated': False},
])
def test_json_types_and_calibration_must_be_valid(record_value):
    source, _, _ = source_with([
        json.dumps({'protocol_version': 1, **record_value}).encode() + b'\n',
    ])
    assert source.poll() is None
    assert source.latest_reading is None
    source.close()


def test_remove_emits_negative_delta():
    source, _, _ = source_with([record(500, sequence=1, uptime=1000),
                                record(480, sequence=2, uptime=1100)], pre_tare=True)
    assert source.poll() is None
    event = source.poll()
    assert event is not None and event.event_type == 'REMOVE'
    assert event.weight_change == -20
    source.close()


def test_unstable_measurements_do_not_set_baseline_or_create_events():
    source, _, _ = source_with([record(500, sequence=1, uptime=1000, stable=False),
                                record(100, sequence=2, uptime=1100),
                                record(115, sequence=3, uptime=1200)], pre_tare=True)
    assert source.poll() is None
    assert source.poll() is None
    event = source.poll()
    assert event is not None and event.weight_change == 15
    source.close()


def test_invalid_sensor_records_and_unstable_samples_do_not_advance_baseline():
    source, device, _ = source_with([record(100, sequence=1, uptime=1000)], pre_tare=True)
    assert source.poll() is None
    assert source._baseline == 100
    device.data.extend(
        b'{"type":"error","code":"weight_out_of_range","protocol_version":1,"sequence":2,"uptime_ms":1100}\n'
    )
    device.data.extend(record(130, sequence=3, uptime=1200, stable=False))
    assert source.poll() is None
    assert source.poll() is None
    assert source._baseline == 100
    device.data.extend(record(115, sequence=4, uptime=1300, stable=True))
    event = source.poll()
    assert event is not None
    assert event.event_type == 'ADD'
    assert event.previous_weight == 100
    assert event.current_weight == 115
    source.close()


def test_duplicate_and_out_of_order_samples_are_rejected():
    source, _, _ = source_with([record(100, sequence=4, uptime=1000),
                                record(120, sequence=4, uptime=1100),
                                record(130, sequence=3, uptime=1200)], pre_tare=True)
    assert source.poll() is None
    assert source.poll() is None
    assert source.poll() is None
    source.close()


def test_stale_backlogged_sample_is_rejected():
    now = [1.0]
    source, _, _ = source_with([record(100, sequence=1, uptime=1000),
                                record(120, sequence=2, uptime=1100)],
                               mono=lambda: now[0], stale_after=2, pre_tare=True)
    assert source.poll() is None
    now[0] = 8.0
    assert source.poll() is None
    source.close()


def test_future_sample_is_still_rejected():
    now = [10.0]
    source, _, _ = source_with([record(100, sequence=1, uptime=1000),
                                record(120, sequence=2, uptime=3000)],
                               mono=lambda: now[0], pre_tare=True)
    assert source.poll() is None
    assert source.poll() is None
    assert source._baseline == 100
    source.close()


def test_serial_timeout_is_no_event_and_does_not_fabricate_measurement():
    source, device, _ = source_with([])
    assert source.poll() is None
    assert source.latest_reading is None
    assert not device.closed
    source.close()
    assert device.closed


def test_disconnect_reconnect_rebaselines_before_emitting_again():
    first = FakeSerial(read_error=OSError('device removed'))
    second = FakeSerial([
        b'ms":8005}\n', ready_record(), error_record('tare_required'),
        record(200, sequence=1, uptime=100), record(230, sequence=2, uptime=200),
    ])
    now = [0.0]
    opened = []
    def factory(**_):
        opened.append(True)
        return first if len(opened) == 1 else second
    source = SerialWeightSource('/dev/fake', serial_factory=factory,
                                monotonic=lambda: now[0], clock=lambda: datetime(2026, 1, 1))
    assert source.poll() is None  # disconnect is contained
    now[0] = 1.1
    assert source.poll() is None  # reconnect does not reuse the old tare or readiness
    assert source._ready is False
    assert source._baseline is None
    source.tare()  # operator confirms empty platform and repeats the handshake/tare
    second.data.extend(record(200, sequence=3, uptime=1000))
    second.data.extend(record(230, sequence=4, uptime=1100))
    assert source.poll() is None  # fresh stable baseline
    assert source.poll() is not None  # only a post-reconnect change becomes an event
    assert len(opened) == 2
    assert first.closed
    source.close()


def test_tare_required_firmware_error_blocks_samples_until_explicit_tare():
    source, device, _ = source_with([
        error_record('tare_required'),
        record(100, sequence=1, uptime=1000),
    ])
    assert source.poll() is None
    assert source._needs_tare is True
    assert source.latest_reading is None
    source.close()


def test_weight_out_of_range_diagnostic_fields_are_reported_but_never_become_reading(caplog):
    source, _, _ = source_with([
        b'{"type":"error","code":"weight_out_of_range","protocol_version":1,"raw":-54142,'
        b'"tare_offset":-54141,"grams":-0.009,"calibration_factor":107.1,'
        b'"sequence":4,"uptime_ms":2000}\n',
    ])
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        assert source.poll() is None
    assert 'raw=-54142' in caplog.text
    assert 'tare_offset=-54141' in caplog.text
    assert 'grams=-0.009' in caplog.text
    assert 'calibration_factor=107.1' in caplog.text
    assert source.latest_reading is None
    source.close()


@pytest.mark.parametrize('invalid_grams', [504.3, 0.0])
def test_zero_raw_sensor_record_cannot_create_add_or_remove_or_change_baseline(invalid_grams, caplog):
    source, device, _ = source_with([record(100, sequence=1, uptime=1000)], pre_tare=True)
    assert source.poll() is None  # initial stable reading sets the baseline
    baseline = source._baseline
    latest = source.latest_reading
    last_sequence = source._last_sequence
    device.data.extend(record(invalid_grams, sequence=2, uptime=2000, raw=0))

    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        assert source.poll() is None

    assert 'zero raw sensor reading' in caplog.text
    assert source._baseline == baseline == 100
    assert source.latest_reading is latest
    assert source._last_sequence == last_sequence == 1
    source.close()


def test_next_valid_record_after_zero_raw_fault_uses_original_baseline():
    now = [10.0]
    source, device, _ = source_with(
        [record(100, sequence=1, uptime=1000)], mono=lambda: now[0], pre_tare=True)
    assert source.poll() is None  # baseline = 100 g
    device.data.extend(record(504.3, sequence=2, uptime=2000, raw=0))
    assert source.poll() is None  # invalid sample is discarded
    assert source._baseline == 100

    now[0] = 12.0
    device.data.extend(record(115, sequence=3, uptime=3000, raw=123500))
    event = source.poll()
    assert event is not None
    assert event.event_type == 'ADD'
    assert event.previous_weight == 100
    assert event.current_weight == 115
    assert event.weight_change == pytest.approx(15)
    assert source._baseline == 115
    source.close()


@pytest.mark.parametrize('grams', [-0.009, -15.0, 20_000.01])
def test_out_of_range_diagnostics_for_zero_noise_material_negative_and_capacity(grams, caplog):
    diagnostic = json.dumps({
        'type': 'error', 'code': 'weight_out_of_range', 'protocol_version': 1, 'raw': 1,
        'tare_offset': 0, 'grams': grams, 'calibration_factor': 107.1,
        'sequence': 2, 'uptime_ms': 200,
    }).encode() + b'\r\n'
    source, _, _ = source_with([diagnostic])
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        assert source.poll() is None
    assert 'weight_out_of_range' in caplog.text
    assert f'grams={grams}' in caplog.text
    assert source.latest_reading is None
    source.close()


def test_older_weight_out_of_range_record_without_diagnostics_remains_compatible(caplog):
    source, _, _ = source_with([b'{"type":"error","code":"weight_out_of_range","protocol_version":1}\n'])
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        assert source.poll() is None
    assert 'HX711 firmware reports sensor state: weight_out_of_range' in caplog.text
    assert source.latest_reading is None
    source.close()


def test_weight_is_rejected_until_firmware_ready_handshake():
    device = FakeSerial([record(100, sequence=1, uptime=1000)])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    assert source.poll() is None
    assert source.latest_reading is None
    source.close()


def test_tare_command_requires_connected_device_resets_baseline_and_closes_cleanly():
    source, device, _ = source_with([
        error_record('tare_required'),
        record(100),
    ])
    assert source.poll() is None
    source.tare()
    assert_handshake_then_tare(device)
    assert source.latest_reading is None
    source.close()
    source.close()
    assert device.closed
    assert source.poll() is None


def test_tare_handshake_handles_repeated_ready_interleaved_with_tare_required():
    device = FakeSerial([
        b'{"type":"status","status":"ready","protocol_version":1,"sequence":0,"uptime_ms":1}\n',
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n',
        b'{"type":"status","status":"ready","protocol_version":1,"sequence":2,"uptime_ms":1005}\n',
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":3,"uptime_ms":2000}\n',
    ])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)

    # The command is only accepted by the fake after the ready record has
    # actually been parsed by this source, not merely observed elsewhere.
    original_write = device.write
    def checked_write(value):
        if value == b'TARE\n':
            assert source._ready is True
        else:
            assert value == f'READY? {source._ready_request_id}\n'.encode()
        return original_write(value)
    device.write = checked_write

    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    assert source._ready is True
    assert source._needs_tare is False
    assert source._tare_result is None  # successful acknowledgement consumed
    assert device.reset == 1  # reset_input_buffer() is called once per _connect()
    source.close()


def test_successful_tare_ack_establishes_and_preserves_clock_anchor():
    now = [12.5]
    wall = datetime(2026, 1, 1, 0, 0, 12, 500000)
    source, device, _ = source_with(
        [b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n'],
        clock=lambda: wall, mono=lambda: now[0], tare_uptime=2000)
    source.tare()
    assert_handshake_then_tare(device)
    assert source._anchor_uptime == 2000
    assert source._anchor_monotonic == 12.5
    assert source._anchor_datetime == wall
    assert source._baseline is None
    assert source._last_sequence is None
    source.close()


def test_delayed_first_poll_uses_tare_anchor_for_queued_then_live_samples():
    now = [10.0]
    wall = datetime(2026, 1, 1)
    source, device, _ = source_with(
        [b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n'],
        clock=lambda: wall, mono=lambda: now[0], tare_uptime=2000)
    source.tare()

    # Simulate camera initialization delay while the board keeps sending at
    # 10 Hz. The queued uptime values must remain anchored to the tare ACK.
    device.data.extend(record(100, sequence=5, uptime=2100))
    device.data.extend(record(100, sequence=6, uptime=2200))
    now[0] = 11.6
    assert source.poll() is None
    first_queued_time = source._measurement_time(source.latest_reading)
    assert first_queued_time == wall + timedelta(seconds=0.1)
    assert first_queued_time < wall + timedelta(seconds=1.6)
    assert source.poll() is None
    second_queued_time = source._measurement_time(source.latest_reading)
    assert second_queued_time == wall + timedelta(seconds=0.2)
    assert second_queued_time < wall + timedelta(seconds=1.6)

    now[0] = 11.8
    device.data.extend(record(115, sequence=7, uptime=3800))
    event = source.poll()
    assert event is not None and event.event_type == 'ADD'
    assert event.timestamp == wall + timedelta(seconds=1.8)
    movement = DetectionEvent(event.timestamp - timedelta(seconds=0.2),
                              'MOVEMENT', (0, 0, 1, 1), 1000)
    correlator = EventOrchestrator()
    assert correlator.on_movement(movement) is None
    confirmed = correlator.on_weight(event)
    assert confirmed is not None
    assert confirmed['time_diff'] == pytest.approx(0.2)
    late_correlator = EventOrchestrator()
    late_movement = DetectionEvent(event.timestamp - timedelta(seconds=4.796),
                                  'MOVEMENT', (0, 0, 1, 1), 1000)
    assert late_correlator.on_movement(late_movement) is None
    assert late_correlator.on_weight(event) is None  # retain the existing 1 s window
    source.close()


def test_tare_required_is_sensor_and_firmware_readiness_evidence():
    device = FakeSerial([
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\r\n',
    ])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    original_write = device.write
    def checked_write(value):
        if value == b'TARE\n':
            assert source._ready and source._sensor_ready
        else:
            assert value == f'READY? {source._ready_request_id}\n'.encode()
        return original_write(value)
    device.write = checked_write

    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    source.close()


def test_tare_waits_through_startup_delay_and_parses_crlf_records():
    class DelayedSerial(FakeSerial):
        def __init__(self):
            super().__init__([])
            self.empty_polls = 0

        @property
        def in_waiting(self):
            if not self.data:
                self.empty_polls += 1
                if self.empty_polls == 4:
                    self.data.extend(
                        b'{"type":"status","status":"ready","protocol_version":1,"sequence":0,"uptime_ms":1}\r\n'
                        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\r\n'
                    )
            return len(self.data)

    device = DelayedSerial()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=0.2)
    assert device.empty_polls >= 4
    assert_handshake_then_tare(device)
    source.close()


def test_readiness_after_nonready_and_unknown_records_then_sends_tare():
    device = FakeSerial([
        b'{malformed\r\n',
        b'{"type":"error","code":"hx711_not_ready","protocol_version":1,"sequence":2,"uptime_ms":1000}\r\n',
        error_record('tare_required'),
    ])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=0.1)
    assert_handshake_then_tare(device)
    source.close()


def test_tare_does_not_succeed_without_positive_firmware_acknowledgement():
    class NoAckSerial(FakeSerial):
        def write(self, value):
            if value.startswith(b'READY? '):
                return super().write(value)
            self.writes.append(value)
            return len(value)

    device = NoAckSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='tare acknowledgement timed out'):
        source.tare(ready_timeout=0.01)
    assert device.writes[-1] == b'TARE\n'
    assert device.writes[0].startswith(b'READY? ')
    assert source._ready is True
    assert source._anchor_uptime is None
    assert source._anchor_monotonic is None
    assert source._anchor_datetime is None
    source.close()


def test_unstable_tare_ack_reports_window_sample_count_and_spread_without_anchor():
    class UnstableTareSerial(FakeSerial):
        def write(self, value):
            if value.startswith(b'READY? '):
                return super().write(value)
            self.writes.append(value)
            if value == b'TARE\n':
                self.data.extend(
                    b'{"type":"tare","ok":false,"protocol_version":1,"reason":"unstable_empty_platform",'
                    b'"sample_count":20,"spread_counts":300,"spread_grams":2.801,'
                    b'"stability_limit_grams":2.0,"uptime_ms":2000}\n'
                )
            return len(value)

    device = UnstableTareSerial([
        ready_record(),
        b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n',
    ])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match=r'samples=20 spread=2\.801g \(300 counts\) limit=2\.000g'):
        source.tare(ready_timeout=0.1)
    assert source._tare_result is False
    assert source._anchor_uptime is None
    assert source._baseline is None
    assert_handshake_then_tare(device)
    source.close()


def test_negative_or_missing_uptime_tare_ack_does_not_establish_anchor():
    class NegativeAckSerial(FakeSerial):
        def write(self, value):
            if value.startswith(b'READY? '):
                return super().write(value)
            self.writes.append(value)
            if value == b'TARE\n':
                self.data.extend(b'{"type":"tare","ok":false,"protocol_version":1,"reason":"hx711_not_ready","uptime_ms":2000}\n')
            return len(value)

    ready = [ready_record(), b'{"type":"error","code":"tare_required","protocol_version":1,"sequence":1,"uptime_ms":1000}\n']
    device = NegativeAckSerial(ready)
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='rejected tare'):
        source.tare()
    assert source._anchor_uptime is None
    assert source._anchor_monotonic is None
    assert source._anchor_datetime is None
    source.close()

    device = FakeSerial(ready)
    def successful_ack_without_uptime(value):
        if value.startswith(b'READY? '):
            return FakeSerial.write(device, value)
        device.writes.append(value)
        if value == b'TARE\n':
            device.data.extend(b'{"type":"tare","ok":true,"protocol_version":1,"reason":"ok"}\n')
        return len(value)
    device.write = successful_ack_without_uptime
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='lacked a valid uptime'):
        source.tare()
    assert len(device.writes) == 2
    assert device.writes[0].startswith(b'READY? ')
    assert device.writes[1] == b'TARE\n'
    assert source._anchor_uptime is None
    assert source._anchor_monotonic is None
    assert source._anchor_datetime is None
    source.close()


def test_ready_heartbeat_without_sensor_ready_never_sends_tare():
    device = FakeSerial([ready_record()])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='sensor readiness is missing') as error:
        source.tare(ready_timeout=0.01)
    assert 'matching READY response received' in str(error.value)
    assert 'tare_required_records=0' in str(error.value)
    assert len(device.writes) == 1 and device.writes[0].startswith(b'READY? ')
    source.close()


def test_disconnect_during_readiness_fails_with_specific_error(caplog):
    device = FakeSerial(read_error=OSError('device removed'))
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        with pytest.raises(RuntimeError, match='serial transport failed before readiness') as error:
            source.tare(ready_timeout=0.1)
    assert isinstance(error.value.__cause__.__cause__, OSError)
    assert 'device removed' in caplog.text
    assert device.closed


def test_serial_read_valueerror_preserves_message_traceback_and_never_tares(caplog):
    device = FakeSerial(read_error=ValueError('driver read rejected after USB reset'))
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        with pytest.raises(RuntimeError, match='serial read operation failed') as error:
            source.tare(ready_timeout=0.1)
    original = error.value.__cause__.__cause__
    assert isinstance(original, ValueError)
    assert str(original) == 'driver read rejected after USB reset'
    assert 'driver read rejected after USB reset' in caplog.text
    assert 'read' in caplog.text
    assert len(device.writes) == 1
    assert device.writes[0].startswith(b'READY? ')
    assert b'TARE\n' not in device.writes
    assert source._baseline is None
    assert source._ready is False


def test_ready_write_valueerror_preserves_cause_and_never_tares(caplog):
    class ValueErrorWriteSerial(FakeSerial):
        def write(self, value):
            self.writes.append(value)
            raise ValueError('serial write rejected')

    device = ValueErrorWriteSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        with pytest.raises(RuntimeError, match='READY request write failed') as error:
            source.tare(ready_timeout=0.1)
    assert isinstance(error.value.__cause__, ValueError)
    assert str(error.value.__cause__) == 'serial write rejected'
    assert 'serial write rejected' in caplog.text
    assert len(device.writes) == 1 and device.writes[0].startswith(b'READY? ')
    assert b'TARE\n' not in device.writes
    assert source._baseline is None
    assert source._ready is False


def test_unexpectedly_closed_port_is_classified_before_handshake_write(caplog):
    device = FakeSerial([ready_record(), error_record('tare_required')])
    device.close()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with caplog.at_level('WARNING', logger='edge.runtime.weight_source'):
        with pytest.raises(RuntimeError, match='serial port access failed') as error:
            source.tare(ready_timeout=0.1)
    assert 'serial factory returned a closed port' in str(error.value)
    assert 'serial factory returned a closed port' in caplog.text
    assert device.writes == []
    assert source._baseline is None
    assert source._ready is False


def test_nonbyte_read_is_framing_error_not_physical_disconnect():
    class NonByteSerial(FakeSerial):
        def read(self, size):
            return 'not bytes'

    device = NonByteSerial([ready_record(), error_record('tare_required')])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError, match='serial framing failed before readiness') as error:
        source.tare(ready_timeout=0.1)
    assert 'expected bytes' in str(error.value)
    assert source._baseline is None
    assert source._ready is False
    assert b'TARE\n' not in device.writes


def test_port_open_failure_is_reported_as_port_access_problem():
    source = SerialWeightSource(
        '/dev/fake', serial_factory=lambda **_: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(RuntimeError, match='serial port access failed'):
        source.tare(ready_timeout=0.1)
    source.close()


def test_default_posix_serial_open_requests_exclusive_reader_lock(monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    captured = []
    device = FakeSerial()
    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(
        Serial=lambda **kwargs: captured.append(kwargs) or device))
    source = SerialWeightSource('/dev/fake')
    assert source._connect()
    if os.name == 'posix':
        assert captured[0]['exclusive'] is True
    else:
        assert 'exclusive' not in captured[0]
    source.close()


def test_unavailable_port_retries_without_crashing_or_simulating():
    now = [0.0]
    calls = []
    def factory(**_):
        calls.append(1)
        raise OSError('not attached')
    source = SerialWeightSource('/dev/missing', serial_factory=factory, monotonic=lambda: now[0])
    assert source.poll() is None
    assert source.poll() is None
    assert len(calls) == 1
    now[0] = 1.1
    assert source.poll() is None
    assert len(calls) == 2
    source.close()


def test_create_weight_source_serial_never_falls_back_and_requires_port():
    with pytest.raises(ValueError, match='serial port'):
        create_weight_source('serial')
    assert isinstance(create_weight_source('stdin'), StdinSimulatedWeightSource)
    from edge.runtime.weight_source import SimulatedWeightSource
    assert isinstance(create_weight_source(), SimulatedWeightSource)
    # Existing factory default remains available; only the runtime CLI requires an explicit source.


def test_cli_requires_exactly_one_source_and_preserves_simulation_flag():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['--cart-id', 'CART-001'])
    with pytest.raises(SystemExit):
        parser.parse_args(['--cart-id', 'CART-001', '--simulate-weight', '--serial-port', '/dev/fake'])
    args = parser.parse_args(['--cart-id', 'CART-001', '--simulate-weight'])
    assert isinstance(select_weight_source(args), StdinSimulatedWeightSource)
    with pytest.raises(SystemExit):
        parse_runtime_args(['--cart-id', 'CART-001', '--serial-port', '/dev/fake'])
    physical = parse_runtime_args(['--cart-id', 'CART-001', '--serial-port', '/dev/fake', '--tare-empty'])
    selected = select_weight_source(physical)
    assert isinstance(selected, SerialWeightSource)
    assert selected.port == '/dev/fake'
    assert selected.baudrate == 115200
    selected.close()


def test_json_lines_can_buffer_multiple_records_without_stalling_later_poll():
    payload = record(100, sequence=1, uptime=1000) + record(120, sequence=2, uptime=1100)
    source, _, _ = source_with([payload], pre_tare=True)
    assert source.poll() is None
    assert source.poll() is not None
    source.close()


# ---------------------------------------------------------------------------
# Chunk-fragmentation tests
#
# These tests verify that _next_line() correctly assembles arbitrary byte
# chunks into complete newline-delimited records, and that _connect() flushes
# stale OS receive-buffer bytes via reset_input_buffer() before reading.
# ---------------------------------------------------------------------------

def _chunk_serial(chunks, stale_prefix=b''):
    """Return a FakeSerial whose data arrives as a list of byte chunks.

    Each element of *chunks* is a bytes object delivered as a separate read()
    call.  reset_input_buffer() discards any pre-loaded bytes (simulating the
    OS kernel-buffer flush), then loads the actual test chunks so they arrive
    after the flush — matching the real-world flow where fresh Arduino bytes
    arrive only after the port-open reset.

    *stale_prefix* can optionally supply bytes that appear before the flush
    (e.g. to simulate OS-retained bytes from a previous session).
    """
    class ChunkedSerial(FakeSerial):
        def __init__(self, chunks, stale_prefix):
            super().__init__([])
            # Any stale bytes are visible before reset_input_buffer() is called.
            self.data.extend(stale_prefix)
            # Fresh chunks are held back until after reset_input_buffer().
            self._pending_chunks = list(chunks)
            self._flushed = False

        def reset_input_buffer(self):
            # Discard all OS-buffered bytes, then make the fresh data available.
            self.data.clear()
            self._flushed = True
            # Load first fresh chunk immediately so in_waiting > 0 right away.
            if self._pending_chunks:
                self.data.extend(self._pending_chunks.pop(0))
            self.reset += 1

        @property
        def in_waiting(self):
            # Load the next fresh chunk when the buffer empties.
            if not self.data and self._flushed and self._pending_chunks:
                self.data.extend(self._pending_chunks.pop(0))
            return len(self.data)

        def read(self, size):
            if not self.is_open:
                raise ValueError('I/O operation on closed serial port')
            result = bytes(self.data[:size])
            del self.data[:size]
            if not self.data and self._flushed and self._pending_chunks:
                self.data.extend(self._pending_chunks.pop(0))
            return result

    return ChunkedSerial(chunks, stale_prefix)


def _tare_source_with_chunks(chunks, *, ready_timeout=2.0, stale_prefix=b''):
    """Open a SerialWeightSource backed by ChunkedSerial and call tare()."""
    device = _chunk_serial(chunks, stale_prefix=stale_prefix)
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=ready_timeout)
    return source, device


def test_chunk_single_record_arrives_byte_by_byte():
    """A JSON record delivered one byte at a time must be reassembled correctly.

    Each byte of ready_record() arrives as a separate read().  The parser must
    accumulate them across many tiny read() calls before finding the newline.
    The tare_required record that establishes sensor readiness arrives as a
    single chunk so the test stays fast while still exercising the byte-by-byte
    assembly path.
    """
    full = error_record('tare_required', sequence=1, uptime=500)
    ready = ready_record()
    # Ready record arrives one byte at a time; tare_required arrives whole.
    chunks = [bytes([b]) for b in ready] + [full]
    source, device = _tare_source_with_chunks(chunks, ready_timeout=2.0)
    assert source._sensor_ready is True
    assert device.reset >= 1, 'reset_input_buffer() must be called on connect'
    source.close()


def test_chunk_single_record_arrives_in_several_partial_reads():
    """A record split across 4 unequal reads must be assembled into one line."""
    full = error_record('tare_required', sequence=2, uptime=800)
    ready = ready_record()
    # Split the tare_required record into 4 unequal pieces.
    n = len(full)
    pieces = [full[:n//4], full[n//4:n//2], full[n//2:3*n//4], full[3*n//4:]]
    chunks = [ready] + pieces
    source, device = _tare_source_with_chunks(chunks)
    assert source._sensor_ready is True
    source.close()


def test_chunk_multiple_records_in_one_read():
    """Two complete records concatenated into a single chunk must both parse."""
    ready = ready_record()
    tare_req = error_record('tare_required', sequence=1, uptime=300)
    # Both records arrive as one chunk.
    chunks = [ready + tare_req]
    source, device = _tare_source_with_chunks(chunks)
    assert source._sensor_ready is True
    source.close()


def test_chunk_partial_record_followed_by_completion_bytes():
    """The first chunk ends mid-record; the second chunk completes it."""
    full = error_record('tare_required', sequence=3, uptime=1200)
    ready = ready_record()
    mid = len(full) // 2
    chunks = [ready, full[:mid], full[mid:]]
    source, device = _tare_source_with_chunks(chunks)
    assert source._sensor_ready is True
    source.close()


def test_chunk_stale_os_buffer_fragment_then_fresh_ready_record():
    """Reproduce the exact production failure: stale fragment first, then valid records.

    macOS retains bytes in the USB-CDC kernel ring buffer across port
    close/open cycles.  The first bytes seen after _connect() may be the
    tail-end of a record from the previous session.  reset_input_buffer() must
    be called to discard them; the fresh boot records that follow must then
    complete the handshake without any interference from the stale bytes.
    """
    stale = b'uence":245,"uptime_ms":122071}\n'  # exact fragment from production failure
    fresh_ready = ready_record()
    fresh_tare = error_record('tare_required', sequence=1, uptime=500)
    # stale_prefix is visible before reset_input_buffer(); fresh chunks arrive after.
    source, device = _tare_source_with_chunks(
        [fresh_ready + fresh_tare],
        stale_prefix=stale,
    )
    assert device.reset >= 1, 'reset_input_buffer() was not called during _connect()'
    assert source._sensor_ready is True
    source.close()


def test_chunk_crlf_line_endings_across_split_chunks():
    """CRLF-terminated records split across two chunks must parse cleanly."""
    def cr(rec):
        return rec.replace(b'\n', b'\r\n')
    ready = cr(ready_record())
    tare_req = cr(error_record('tare_required', sequence=1, uptime=400))
    mid = len(tare_req) // 2
    chunks = [ready, tare_req[:mid], tare_req[mid:]]
    source, device = _tare_source_with_chunks(chunks)
    assert source._sensor_ready is True
    source.close()


def test_chunk_malformed_record_is_rejected_but_subsequent_valid_record_accepted():
    """A single malformed chunk must not corrupt the stream for the record that follows."""
    ready = ready_record()
    garbage = b'NOT JSON AT ALL\n'
    tare_req = error_record('tare_required', sequence=1, uptime=600)
    # ready → garbage → tare_required; the garbage increments consecutive but
    # tare_required resets it and establishes sensor readiness.
    chunks = [ready, garbage, tare_req]
    source, device = _tare_source_with_chunks(chunks)
    assert source._sensor_ready is True
    assert source._consecutive_malformed == 0, 'valid record after garbage must reset counter'
    source.close()


def test_chunk_oversized_record_raises_framing_error_not_silent_truncation():
    """A record exceeding MAX_LINE_BYTES without a newline must raise, not silently drop."""
    oversized = b'X' * (SerialWeightSource.MAX_LINE_BYTES + 1)  # no newline
    device = _chunk_serial([oversized])
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    with pytest.raises(RuntimeError):
        source.tare(ready_timeout=0.2)
    source.close()


def test_chunk_delayed_versioned_ready_response_completes_handshake():
    """The READY? response may arrive many records after the request; handshake still completes."""
    # Simulate firmware emitting several heartbeats before echoing the request_id.
    class DelayedNonceSerial(FakeSerial):
        def __init__(self):
            super().__init__([error_record('tare_required', sequence=1, uptime=200)])
            self.nonce_writes = 0

        def write(self, value):
            if value.startswith(b'READY? '):
                # Inject three heartbeats without request_id, then the matched response.
                for i in range(3):
                    hb = {'type': 'status', 'status': 'ready', 'protocol_version': 1,
                          'sequence': 10 + i, 'uptime_ms': 3000 + i * 100}
                    self.data.extend(json.dumps(hb).encode() + b'\n')
                self.nonce_writes += 1
            return super().write(value)

    device = DelayedNonceSerial()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=1.0)
    assert source._ready is True
    assert source._sensor_ready is True
    assert source._readiness_heartbeats_without_id == 3
    source.close()


def test_chunk_repeated_identical_ready_request_ids_only_satisfy_once():
    """Replaying the same READY? nonce response must not double-satisfy the handshake."""
    class DuplicateNonceSerial(FakeSerial):
        def write(self, value):
            if value.startswith(b'READY? '):
                request_id = int(value[len(b'READY? '):].strip())
                # Emit the matched response twice in a row.
                for _ in range(2):
                    resp = {'type': 'status', 'status': 'ready', 'protocol_version': 1,
                            'sequence': 5, 'uptime_ms': 1000, 'request_id': request_id}
                    self.data.extend(json.dumps(resp).encode() + b'\n')
                tare_resp = {'type': 'error', 'code': 'tare_required',
                             'protocol_version': 1, 'sequence': 6, 'uptime_ms': 1100}
                self.data.extend(json.dumps(tare_resp).encode() + b'\n')
            elif value == b'TARE\n':
                resp = {'type': 'tare', 'ok': True, 'protocol_version': 1,
                        'reason': 'ok', 'sample_count': 20, 'spread_counts': 10,
                        'spread_grams': 0.09, 'stability_limit_grams': 2.0,
                        'sequence': 7, 'uptime_ms': 1200}
                self.data.extend(json.dumps(resp).encode() + b'\n')
            self.writes.append(value)
            return len(value)

    device = DuplicateNonceSerial()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=1.0)
    # Exactly one READY? write, one TARE write; the duplicate nonce did not cause a second send.
    assert device.writes.count(b'TARE\n') == 1
    assert sum(1 for w in device.writes if w.startswith(b'READY? ')) >= 1
    assert source._ready is True
    source.close()


def test_connect_always_calls_reset_input_buffer_on_every_open():
    """reset_input_buffer() must be called each time _connect() opens the port.

    This is the direct regression test for the production failure: stale OS
    receive-buffer bytes (uptime_ms ~122 s) caused 64 consecutive malformed
    records because reset_input_buffer() was never called.
    """
    call_count = []

    class CountingResetSerial(FakeSerial):
        def __init__(self):
            super().__init__([ready_record(), error_record('tare_required')])

        def reset_input_buffer(self):
            call_count.append(1)
            super().reset_input_buffer()

    device = CountingResetSerial()
    source = SerialWeightSource('/dev/fake', serial_factory=lambda **_: device)
    source.tare(ready_timeout=0.5)
    assert len(call_count) == 1, (
        'reset_input_buffer() must be called exactly once per _connect(); '
        f'was called {len(call_count)} time(s)'
    )
    source.close()
