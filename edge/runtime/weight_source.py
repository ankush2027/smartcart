"""Weight inputs for development and the HX711 USB-serial adapter."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import math
import os
import secrets
import select
import sys
import time
import traceback
from typing import Protocol

from edge.weight.weight import WeightSensor
from edge.weight.weight_event import WeightEvent, detect_weight_change

logger = logging.getLogger(__name__)


class WeightSource(Protocol):
    def poll(self) -> WeightEvent | None: ...
    def close(self) -> None: ...


class SimulatedWeightSource:
    """Convert injected signed gram changes through the existing weight boundary."""
    def __init__(self, initial_weight=500.0, clock=datetime.now):
        if not math.isfinite(initial_weight) or initial_weight < 0:
            raise ValueError('Initial simulated weight must be finite and nonnegative')
        self.clock = clock
        self.sensor = WeightSensor()
        self.sensor.current_weight = initial_weight
        self.baseline = self.sensor.read_weight()
        self._last_event = None

    def inject_delta(self, delta):
        if not math.isfinite(delta):
            raise ValueError('Weight change must be finite')
        if self.sensor.read_weight() + delta < 0:
            raise ValueError('Simulated weight cannot become negative')
        self.sensor.simulate_change(delta)
        current = self.sensor.read_weight()
        event = detect_weight_change(self.baseline, current)
        if event is not None:
            event.timestamp = self.clock()
            self.baseline = current
            self._last_event = event
        return event

    def poll(self):
        event, self._last_event = self._last_event, None
        return event

    def close(self):
        pass


class StdinSimulatedWeightSource(SimulatedWeightSource):
    """Nonblocking terminal polling; enter one signed gram delta per line."""
    def __init__(self, initial_weight=500.0, clock=datetime.now):
        super().__init__(initial_weight, clock)
        self._pending = ''

    def poll(self):
        # readline() can block on a partial pipe line even after select().
        # Read available bytes only so input never stalls camera acquisition.
        if '\n' not in self._pending:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if not readable:
                return None
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                raise EOFError('Simulation input closed')
            self._pending += chunk.decode('ascii')
            if len(self._pending) > 8192:
                raise ValueError('Simulation input line too long')
        if '\n' not in self._pending:
            return None
        line, self._pending = self._pending.split('\n', 1)
        line = line.strip()
        if not line:
            return None  # Blank record is not a delta or EOF.
        return self.inject_delta(float(line))


@dataclass(frozen=True)
class SerialWeightReading:
    grams: float
    raw: int
    stable: bool
    sequence: int
    uptime_ms: int


class _MalformedHX711Stream(RuntimeError):
    """Raised when a serial stream exceeds the bounded framing-recovery budget."""


class _SerialTransportError(RuntimeError):
    """A serial port operation failed or the port was closed unexpectedly."""


class _SerialProtocolError(RuntimeError):
    """The serial read returned bytes that violate the bounded JSONL framing contract."""


class SerialWeightSource:
    """Read validated newline-delimited HX711 measurements from USB serial.

    The adapter deliberately does not simulate values. A disconnected port,
    timeout, invalid record or unstable reading produces no WeightEvent.
    Serial reads are non-blocking so camera acquisition is not held up waiting
    for the slower HX711 sampling rate.
    """

    BAUDRATE = 115200
    SERIAL_TIMEOUT = 0.1
    MAX_LINE_BYTES = 2048
    RETRY_INTERVAL = 1.0
    READY_STARTUP_GRACE = 2.0
    READY_RETRY_INTERVAL = 0.5
    EVENT_THRESHOLD_GRAMS = 10.0
    UINT32 = 1 << 32
    PROTOCOL_VERSION = 1
    MAX_CONSECUTIVE_MALFORMED_RECORDS = 64
    MALFORMED_WARNING_INTERVAL = 5.0
    SENSOR_WARNING_INTERVAL = 5.0

    def __init__(self, port: str, baudrate: int = BAUDRATE, timeout: float = SERIAL_TIMEOUT,
                 *, max_weight_grams: float = 20_000.0, stale_after: float = 2.0,
                 clock=datetime.now, monotonic=time.monotonic, serial_factory=None) -> None:
        if not isinstance(port, str) or not port.strip():
            raise ValueError('A serial port is required for physical weight input')
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError('Serial baudrate must be a positive integer')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Serial timeout must be positive and finite')
        if not isinstance(max_weight_grams, (int, float)) or not math.isfinite(max_weight_grams) or max_weight_grams <= 0:
            raise ValueError('Maximum weight must be positive and finite')
        if not isinstance(stale_after, (int, float)) or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError('Stale-reading limit must be positive and finite')
        self.port = port.strip()
        self.baudrate = baudrate
        self.timeout = float(timeout)
        self.max_weight_grams = float(max_weight_grams)
        self.stale_after = float(stale_after)
        self._clock = clock
        self._monotonic = monotonic
        self._serial_factory = serial_factory
        self._serial = None
        self._startup_grace_until: float | None = None
        self._closed = False
        self._retry_at = 0.0
        self._buffer = bytearray()
        self._discard_first_record = False
        self._consecutive_malformed = 0
        self._last_malformed_warning: float | None = None
        self._ready = False
        self._ready_request_id: int | None = None
        self._readiness_status_records = 0
        self._readiness_heartbeats_without_id = 0
        self._readiness_mismatched_ids = 0
        self._readiness_protocol_mismatches = 0
        self._readiness_invalid_status_records = 0
        self._readiness_tare_required_records = 0
        self._last_sensor_log_code: str | None = None
        self._last_sensor_log_at: float | None = None
        self._sensor_ready = False
        self._sensor_error: str | None = None
        self._connection_error: str | None = None
        self._needs_tare = False
        self._tare_result: bool | None = None
        self._tare_reason: str | None = None
        self._tare_diagnostic: dict | None = None
        self._tare_command_pending = False
        self._tare_ack_uptime: int | None = None
        self._tare_ack_monotonic: float | None = None
        self._tare_ack_datetime: datetime | None = None
        self._last_sequence: int | None = None
        self._last_uptime: int | None = None
        self._anchor_uptime: int | None = None
        self._anchor_monotonic: float | None = None
        self._anchor_datetime: datetime | None = None
        self._baseline: float | None = None
        self.latest_reading: SerialWeightReading | None = None

    def _connect(self) -> bool:
        if self._closed:
            return False
        if self._serial is not None:
            return True
        now = self._monotonic()
        if now < self._retry_at:
            return False
        factory = self._serial_factory
        open_options = {
            'port': self.port,
            'baudrate': self.baudrate,
            'timeout': self.timeout,
        }
        if factory is None:
            try:
                import serial
            except ImportError:
                raise RuntimeError('Physical weight input requires pyserial; install declared requirements manually') from None
            factory = serial.Serial
            # POSIX pySerial uses flock(LOCK_EX | LOCK_NB) when exclusive is
            # requested. This prevents two SmartCart/pySerial readers from
            # competing for the Arduino's status and tare-ack lines.
            if os.name == 'posix':
                open_options['exclusive'] = True
        try:
            self._serial = factory(**open_options)
            if getattr(self._serial, 'is_open', True) is False:
                raise _SerialTransportError('serial factory returned a closed port')
            self._startup_grace_until = self._monotonic() + self.READY_STARTUP_GRACE
            # macOS (and Linux) do NOT flush the USB-CDC kernel receive buffer
            # when the application closes the port.  Bytes that the Arduino
            # transmitted during a previous session — including any partial
            # record that was in-flight when that session ended — remain queued
            # and are handed to the next opener via in_waiting.  A stale
            # partial record causes _invalid_serial_record() to fire on every
            # line until the consecutive limit is reached.
            #
            # reset_input_buffer() issues TCIFLUSH (POSIX) / PurgeComm
            # (Windows) which discards all bytes currently in the kernel
            # receive buffer.  This is safe here because any startup JSON from
            # a prior session is stale and irrelevant; the grace window that
            # follows will capture the fresh boot records the MCU emits after
            # its DTR-triggered reset.
            reset_fn = getattr(self._serial, 'reset_input_buffer', None)
            if callable(reset_fn):
                reset_fn()
            self._reset_measurement_state()
            # The MCU resets on DTR toggle when the port is opened.  The very
            # first line may still be a partial fragment if the USB-CDC bridge
            # transmitted a few bytes before the reset propagated to the AVR
            # TX FIFO.  Discard that one line gracefully.
            self._discard_first_record = True
            self._consecutive_malformed = 0
            self._ready = False
            self._ready_request_id = None
            self._reset_readiness_diagnostics()
            self._sensor_ready = False
            self._sensor_error = None
            self._connection_error = None
            self._needs_tare = False
            self._tare_result = None
            self._tare_reason = None
            self._tare_diagnostic = None
            self._tare_command_pending = False
            self._tare_ack_uptime = None
            self._tare_ack_monotonic = None
            self._tare_ack_datetime = None
            logger.info('HX711 serial input connected on %s at %d baud', self.port, self.baudrate)
            return True
        except Exception as error:
            self._serial = None
            self._retry_at = now + self.RETRY_INTERVAL
            self._connection_error = self._exception_summary(error)
            self._log_serial_failure('open', error)
            return False

    def _reset_measurement_state(self, *, preserve_clock_anchor: bool = False) -> None:
        self._buffer.clear()
        self._last_sequence = None
        self._last_uptime = None
        if not preserve_clock_anchor:
            self._anchor_uptime = None
            self._anchor_monotonic = None
            self._anchor_datetime = None
        self._baseline = None
        self.latest_reading = None

    @staticmethod
    def _exception_summary(error: BaseException, max_chars: int = 240) -> str:
        detail = str(error).replace('\n', ' ').replace('\r', ' ')
        if len(detail) > max_chars:
            detail = detail[:max_chars - 3] + '...'
        return f'{type(error).__name__}: {detail}'

    def _log_serial_failure(self, operation: str, error: BaseException) -> None:
        diagnostic = ''.join(traceback.TracebackException.from_exception(
            error, limit=8, capture_locals=False, lookup_lines=False).format())
        if len(diagnostic) > 1800:
            diagnostic = diagnostic[-1800:]
        logger.warning(
            'HX711 serial %s failed on %s (%s)\n%s',
            operation, self.port, self._exception_summary(error), diagnostic)

    def _disconnect(self, error, *, operation: str = 'transport') -> None:
        connection, self._serial = self._serial, None
        self._startup_grace_until = None
        self._retry_at = self._monotonic() + self.RETRY_INTERVAL
        self._reset_measurement_state()
        self._ready = False
        self._ready_request_id = None
        self._sensor_ready = False
        self._sensor_error = None
        self._tare_result = None
        self._tare_reason = None
        self._tare_diagnostic = None
        self._tare_command_pending = False
        self._tare_ack_uptime = None
        self._tare_ack_monotonic = None
        self._tare_ack_datetime = None
        close_error = None
        if connection is not None:
            try:
                connection.close()
            except Exception as error_while_closing:
                close_error = error_while_closing
        self._log_serial_failure(operation, error)
        if close_error is not None:
            self._log_serial_failure('close after failure', close_error)
        logger.warning('HX711 serial input stopped on %s; readings paused (%s)',
                       self.port, self._exception_summary(error))

    def _ensure_serial_open(self, operation: str) -> None:
        connection = self._serial
        if connection is None:
            raise _SerialTransportError(f'serial connection is not open before {operation}')
        try:
            is_open = getattr(connection, 'is_open', True)
        except Exception as error:
            raise _SerialTransportError(
                f'checking serial port state before {operation} failed: '
                f'{self._exception_summary(error)}') from error
        if is_open is False:
            raise _SerialTransportError(f'serial object reports is_open=False before {operation}')

    def _next_line(self) -> bytes | None:
        connection = self._serial
        self._ensure_serial_open('read')
        newline = self._buffer.find(b'\n')
        if newline < 0:
            try:
                waiting = getattr(connection, 'in_waiting', None)
                if waiting is not None:
                    if waiting <= 0:
                        return None
                    chunk = connection.read(waiting)
                else:
                    chunk = connection.readline()
            except Exception as error:
                raise _SerialTransportError(
                    f'serial read operation failed: {self._exception_summary(error)}') from error
            if not isinstance(chunk, (bytes, bytearray)):
                raise _SerialProtocolError(
                    f'serial read returned {type(chunk).__name__}, expected bytes')
            self._buffer.extend(chunk)
            if len(self._buffer) > self.MAX_LINE_BYTES and b'\n' not in self._buffer:
                self._buffer.clear()
                raise _SerialProtocolError(
                    f'serial JSONL record exceeded {self.MAX_LINE_BYTES} bytes before newline')
            newline = self._buffer.find(b'\n')
            if newline < 0:
                return None
        line = bytes(self._buffer[:newline]).rstrip(b'\r')
        del self._buffer[:newline + 1]
        if not line:
            return None
        return line

    @staticmethod
    def _uint32(value) -> bool:
        return type(value) is int and 0 <= value < SerialWeightSource.UINT32

    def _invalid_serial_record(self, raw_line: bytes, reason: str):
        self._consecutive_malformed += 1
        now = self._monotonic()
        if (self._last_malformed_warning is None
                or now - self._last_malformed_warning >= self.MALFORMED_WARNING_INTERVAL
                or self._consecutive_malformed >= self.MAX_CONSECUTIVE_MALFORMED_RECORDS):
            logger.warning(
                'Invalid HX711 serial record (%s; consecutive=%d; sample=%r)',
                reason, self._consecutive_malformed, raw_line[:80])
            self._last_malformed_warning = now
        if self._consecutive_malformed >= self.MAX_CONSECUTIVE_MALFORMED_RECORDS:
            raise _MalformedHX711Stream(
                f'{self._consecutive_malformed} consecutive invalid HX711 serial records')
        return None

    def _valid_serial_record(self):
        self._consecutive_malformed = 0

    def _reset_readiness_diagnostics(self):
        self._readiness_status_records = 0
        self._readiness_heartbeats_without_id = 0
        self._readiness_mismatched_ids = 0
        self._readiness_protocol_mismatches = 0
        self._readiness_invalid_status_records = 0
        self._readiness_tare_required_records = 0
        self._last_sensor_log_code = None
        self._last_sensor_log_at = None

    def _log_sensor_status(self, code: str, message: str, *args) -> None:
        now = self._monotonic()
        if (code != self._last_sensor_log_code
                or self._last_sensor_log_at is None
                or now - self._last_sensor_log_at >= self.SENSOR_WARNING_INTERVAL):
            logger.warning(message, *args)
            self._last_sensor_log_code = code
            self._last_sensor_log_at = now

    def _discard_startup_fragment(self, raw_line: bytes, reason: str):
        self._discard_first_record = False
        logger.info(
            'Resynchronized HX711 serial stream after port open '
            '(discarded initial fragment: %s; bytes=%d; sample=%r)',
            reason, len(raw_line), raw_line[:80])
        return None

    def _parse_weight(self, raw_line: bytes):
        try:
            record = json.loads(raw_line.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if self._discard_first_record:
                return self._discard_startup_fragment(raw_line, 'not valid UTF-8 JSON')
            return self._invalid_serial_record(raw_line, 'not valid UTF-8 JSON')
        if not isinstance(record, dict):
            if self._discard_first_record:
                return self._discard_startup_fragment(raw_line, 'JSON value is not an object')
            return self._invalid_serial_record(raw_line, 'JSON value is not an object')
        if (type(record.get('protocol_version')) is not int
                or record['protocol_version'] != self.PROTOCOL_VERSION):
            if self._ready_request_id is not None:
                self._readiness_protocol_mismatches += 1
            return self._invalid_serial_record(raw_line, 'unsupported or missing protocol version')
        if record.get('type') not in {'status', 'error', 'tare', 'weight'}:
            if self._discard_first_record:
                return self._discard_startup_fragment(raw_line, 'unrecognized initial JSON record')
            return self._invalid_serial_record(raw_line, 'unrecognized JSON record type')
        self._discard_first_record = False
        if record.get('type') == 'status':
            if record.get('status') == 'ready':
                if (not self._uint32(record.get('sequence'))
                        or not self._uint32(record.get('uptime_ms'))):
                    if self._ready_request_id is not None:
                        self._readiness_invalid_status_records += 1
                    return self._invalid_serial_record(raw_line, 'ready status lacks valid counters')
                request_id = record.get('request_id')
                if request_id is not None and not self._uint32(request_id):
                    if self._ready_request_id is not None:
                        self._readiness_invalid_status_records += 1
                    return self._invalid_serial_record(raw_line, 'ready status has invalid request id')
                self._valid_serial_record()
                if self._ready_request_id is not None:
                    self._readiness_status_records += 1
                    if request_id is None:
                        self._readiness_heartbeats_without_id += 1
                    elif request_id == self._ready_request_id:
                        self._ready = True
                    else:
                        self._readiness_mismatched_ids += 1
                return None
            if self._ready_request_id is not None:
                self._readiness_invalid_status_records += 1
            return self._invalid_serial_record(raw_line, 'unknown status record')
        if record.get('type') == 'error':
            code = record.get('code')
            if code not in {'tare_required', 'hx711_not_ready',
                            'calibration_required', 'invalid_calibration_factor',
                            'weight_out_of_range'}:
                return self._invalid_serial_record(raw_line, 'unknown firmware error code')
            if (code == 'tare_required'
                    and (not self._uint32(record.get('sequence'))
                         or not self._uint32(record.get('uptime_ms')))):
                return self._invalid_serial_record(raw_line, 'tare_required lacks valid counters')
            self._valid_serial_record()
            if code == 'tare_required':
                # Firmware emits this only after scale.is_ready() and
                # calibration validation pass. It proves sensor readiness;
                # only a matching READY? response proves this host handshake.
                self._sensor_ready = True
                if self._ready_request_id is not None:
                    self._readiness_tare_required_records += 1
                self._sensor_error = None
                self._needs_tare = True
                self._baseline = None
                self.latest_reading = None
            elif code == 'hx711_not_ready':
                self._sensor_ready = False
                self._sensor_error = code
            elif code in {'calibration_required', 'invalid_calibration_factor'}:
                self._sensor_ready = False
                self._sensor_error = code
            elif code == 'weight_out_of_range':
                # This can occur after a prior tare; the HX711 itself is
                # responding, so an explicit re-tare may recover the offset.
                self._sensor_ready = True
                self._sensor_error = None
            if code in {'hx711_not_ready', 'invalid_calibration_factor',
                        'calibration_required', 'tare_required', 'weight_out_of_range'}:
                if (code == 'weight_out_of_range'
                        and all(key in record for key in
                                ('raw', 'tare_offset', 'grams', 'calibration_factor'))):
                    self._log_sensor_status(
                        code,
                        'HX711 firmware reports sensor state: %s '
                        '(raw=%r tare_offset=%r grams=%r calibration_factor=%r)',
                        code, record['raw'], record['tare_offset'],
                        record['grams'], record['calibration_factor'])
                else:
                    self._log_sensor_status(
                        code, 'HX711 firmware reports sensor state: %s', code)
            else:
                logger.warning('HX711 firmware reported an invalid error record')
            return None
        if record.get('type') == 'tare':
            if type(record.get('ok')) is bool:
                self._valid_serial_record()
                self._tare_result = record['ok']
                self._tare_reason = record.get('reason') if isinstance(record.get('reason'), str) else None
                sample_count = record.get('sample_count')
                spread_counts = record.get('spread_counts')
                spread_grams = record.get('spread_grams')
                stability_limit = record.get('stability_limit_grams')
                if (type(sample_count) is int and sample_count >= 0
                        and type(spread_counts) is int and spread_counts >= 0
                        and type(spread_grams) in (int, float)
                        and math.isfinite(spread_grams)
                        and type(stability_limit) in (int, float)
                        and math.isfinite(stability_limit)):
                    self._tare_diagnostic = {
                        'sample_count': sample_count,
                        'spread_counts': spread_counts,
                        'spread_grams': float(spread_grams),
                        'stability_limit_grams': float(stability_limit),
                    }
                else:
                    self._tare_diagnostic = None
                if self._tare_command_pending and record['ok']:
                    uptime = record.get('uptime_ms')
                    if self._uint32(uptime):
                        self._tare_ack_uptime = uptime
                        self._tare_ack_monotonic = self._monotonic()
                        self._tare_ack_datetime = self._clock()
            else:
                return self._invalid_serial_record(raw_line, 'tare acknowledgement lacks boolean ok')
            return None
        grams, stable, raw, sequence, uptime = (
            record.get('grams'), record.get('stable'), record.get('raw'),
            record.get('sequence'), record.get('uptime_ms'))
        if record.get('type') == 'weight' and raw == 0:
            # Zero is the observed HX711 read fault for this hardware setup.
            # Reject it before poll() can update sequence, latest_reading, or
            # the stable-weight baseline; never reinterpret it as zero grams.
            return self._invalid_serial_record(raw_line, 'zero raw sensor reading')
        valid_grams = (type(grams) in (int, float) and math.isfinite(grams)
                       and 0 <= grams <= self.max_weight_grams)
        if (record.get('type') != 'weight' or not self._ready or self._needs_tare or not valid_grams
                or type(raw) is not int or not -(1 << 31) <= raw < (1 << 31)
                or type(stable) is not bool or record.get('calibrated') is not True
                or not self._uint32(sequence) or not self._uint32(uptime)):
            return self._invalid_serial_record(raw_line, 'invalid or impossible measurement')
        self._valid_serial_record()
        self._sensor_ready = True
        self._sensor_error = None
        return SerialWeightReading(float(grams), raw, stable, sequence, uptime)

    def _measurement_time(self, reading: SerialWeightReading):
        now_mono = self._monotonic()
        if self._anchor_uptime is None:
            self._anchor_uptime = reading.uptime_ms
            self._anchor_monotonic = now_mono
            self._anchor_datetime = self._clock()
        elapsed_ms = (reading.uptime_ms - self._anchor_uptime) % self.UINT32
        sample_mono = self._anchor_monotonic + elapsed_ms / 1000.0
        age = now_mono - sample_mono
        if age > self.stale_after or age < -0.5:
            logger.warning('Ignoring stale/future HX711 measurement (estimated age %.3fs)', age)
            return None
        return self._anchor_datetime + timedelta(seconds=elapsed_ms / 1000.0)

    def poll(self) -> WeightEvent | None:
        if self._closed or not self._connect():
            return None
        reading = None
        for _ in range(8):  # consume bounded status/error records before a sample
            try:
                line = self._next_line()
            except Exception as error:
                self._disconnect(error, operation='poll read/framing')
                return None
            if line is None:
                return None  # serial timeout / partial line: no fabricated sample
            try:
                reading = self._parse_weight(line)
            except _MalformedHX711Stream as error:
                self._disconnect(error)
                raise RuntimeError(
                    f'{error}; physical weight input stopped; inspect the serial stream before restarting'
                ) from None
            if reading is not None:
                break
        if reading is None:
            return None
        if self._last_sequence is not None:
            sequence_delta = (reading.sequence - self._last_sequence) % self.UINT32
            uptime_delta = (reading.uptime_ms - self._last_uptime) % self.UINT32
            if not (0 < sequence_delta < (1 << 31) and 0 < uptime_delta < (1 << 31)):
                logger.warning('Ignoring duplicate or out-of-order HX711 sequence')
                return None
        self._last_sequence = reading.sequence
        self._last_uptime = reading.uptime_ms
        sample_time = self._measurement_time(reading)
        self.latest_reading = reading
        if sample_time is None or not reading.stable:
            return None
        if self._baseline is None:
            self._baseline = reading.grams
            return None  # initial stable sample is a baseline, never an event
        event = detect_weight_change(self._baseline, reading.grams,
                                     threshold=self.EVENT_THRESHOLD_GRAMS)
        if event is not None:
            event.timestamp = sample_time
            self._baseline = reading.grams
        return event

    def tare(self, ready_timeout: float = 5.0) -> None:
        """Request a tare only after an operator confirms the platform is empty.

        Requests a versioned readiness response tied to this connection so a
        buffered boot record cannot satisfy the handshake by itself.
        """
        if not math.isfinite(ready_timeout) or ready_timeout <= 0:
            raise ValueError('Firmware readiness timeout must be positive and finite')
        if not self._connect() or self._serial is None:
            detail = f' ({self._connection_error})' if self._connection_error else ''
            raise RuntimeError(
                'HX711 serial port access failed or the port is already owned '
                f'by another reader{detail}; tare was not sent'
            )
        self._ready = False
        self._ready_request_id = None
        self._reset_readiness_diagnostics()
        # USB serial open resets the Mega. Drain complete startup records during
        # this bounded grace window before issuing the correlated READY request.
        grace_until = self._startup_grace_until or self._monotonic()
        while self._monotonic() < grace_until:
            try:
                line = self._next_line()
            except (_SerialTransportError, _SerialProtocolError) as error:
                self._disconnect(error, operation='read firmware startup')
                raise RuntimeError(
                    f'HX711 serial failed during startup grace ({self._exception_summary(error)}); '
                    'TARE was not sent'
                ) from error
            if line is not None:
                try:
                    self._parse_weight(line)
                except _MalformedHX711Stream as error:
                    self._disconnect(error, operation='validate startup stream')
                    raise RuntimeError(
                        f'{error}; TARE was not sent; verify the selected firmware and serial port'
                    ) from None
                if self._sensor_error in {'calibration_required', 'invalid_calibration_factor'}:
                    raise RuntimeError(
                        f'HX711 firmware reports {self._sensor_error}; tare was not sent'
                    )
            else:
                time.sleep(min(0.01, max(0.0, grace_until - self._monotonic())))

        self._ready_request_id = secrets.randbits(32)
        ready_command = f'READY? {self._ready_request_id}\n'.encode('ascii')
        deadline = self._monotonic() + ready_timeout
        next_ready_retry = deadline
        try:
            self._ensure_serial_open('READY write')
            self._serial.write(ready_command)
            flush = getattr(self._serial, 'flush', None)
            if callable(flush):
                flush()
            next_ready_retry = self._monotonic() + self.READY_RETRY_INTERVAL
        except Exception as error:
            self._disconnect(error, operation='write READY request')
            raise RuntimeError(
                f'HX711 READY request write failed ({self._exception_summary(error)}); '
                'TARE was not sent'
            ) from error
        while not (self._ready and self._sensor_ready) and self._monotonic() < deadline:
            try:
                line = self._next_line()
            except _SerialTransportError as error:
                self._disconnect(error, operation='read readiness response')
                raise RuntimeError(
                    f'HX711 serial transport failed before readiness: '
                    f'{self._exception_summary(error)}'
                ) from error
            except _SerialProtocolError as error:
                self._disconnect(error, operation='validate readiness framing')
                raise RuntimeError(
                    f'HX711 serial framing failed before readiness: '
                    f'{self._exception_summary(error)}'
                ) from error
            if line is not None:
                try:
                    self._parse_weight(line)  # consumes status/error; no weight can precede tare
                except _MalformedHX711Stream as error:
                    self._disconnect(error, operation='validate readiness stream')
                    raise RuntimeError(
                        f'{error}; TARE was not sent; verify the selected firmware and serial port'
                    ) from None
                if self._sensor_error in {'calibration_required', 'invalid_calibration_factor'}:
                    raise RuntimeError(
                        f'HX711 firmware reports {self._sensor_error}; tare was not sent'
                    )
            else:
                now = self._monotonic()
                if now >= next_ready_retry and now < deadline:
                    try:
                        self._ensure_serial_open('READY retry write')
                        self._serial.write(ready_command)
                        flush = getattr(self._serial, 'flush', None)
                        if callable(flush):
                            flush()
                        next_ready_retry = now + self.READY_RETRY_INTERVAL
                    except Exception as error:
                        self._disconnect(error, operation='write READY retry')
                        raise RuntimeError(
                            f'HX711 READY retry failed ({self._exception_summary(error)}); '
                            'TARE was not sent'
                        ) from error
                else:
                    sleep_until_retry = max(0.0, next_ready_retry - now)
                    time.sleep(min(0.01, sleep_until_retry, max(0.0, deadline - now)))
        if not (self._ready and self._sensor_ready):
            if not self._ready:
                reason = 'no matching versioned READY response'
            else:
                detail = f'; last_sensor_status={self._sensor_error}' if self._sensor_error else ''
                reason = f'matching READY response received but sensor readiness is missing{detail}'
            diagnostics = (
                f'status_records={self._readiness_status_records}, '
                f'heartbeats_without_request_id={self._readiness_heartbeats_without_id}, '
                f'mismatched_request_ids={self._readiness_mismatched_ids}, '
                f'protocol_version_mismatches={self._readiness_protocol_mismatches}, '
                f'invalid_status_records={self._readiness_invalid_status_records}, '
                f'tare_required_records={self._readiness_tare_required_records}'
            )
            raise RuntimeError(
                f'HX711 readiness timed out ({reason}; {diagnostics}); TARE was not sent'
            )
        self._tare_result = None
        self._tare_diagnostic = None
        self._tare_command_pending = True
        self._tare_ack_uptime = None
        self._tare_ack_monotonic = None
        self._tare_ack_datetime = None
        self._anchor_uptime = None
        self._anchor_monotonic = None
        self._anchor_datetime = None
        try:
            self._ensure_serial_open('TARE write')
            self._serial.write(b'TARE\n')
            flush = getattr(self._serial, 'flush', None)
            if callable(flush):
                flush()
        except Exception as error:
            self._disconnect(error, operation='write TARE command')
            raise RuntimeError(
                f'HX711 TARE command write failed ({self._exception_summary(error)})'
            ) from error
        deadline = self._monotonic() + ready_timeout
        while self._tare_result is None and self._monotonic() < deadline:
            try:
                line = self._next_line()
            except _SerialTransportError as error:
                self._disconnect(error, operation='read tare acknowledgement')
                raise RuntimeError(
                    f'HX711 serial transport failed before tare acknowledgement: '
                    f'{self._exception_summary(error)}'
                ) from error
            except _SerialProtocolError as error:
                self._disconnect(error, operation='validate tare acknowledgement framing')
                raise RuntimeError(
                    f'HX711 serial framing failed before tare acknowledgement: '
                    f'{self._exception_summary(error)}'
                ) from error
            if line is not None:
                try:
                    self._parse_weight(line)
                except _MalformedHX711Stream as error:
                    self._disconnect(error, operation='validate tare acknowledgement stream')
                    raise RuntimeError(
                        f'{error}; tare acknowledgement could not be read'
                    ) from None
            else:
                time.sleep(min(0.01, max(0.0, deadline - self._monotonic())))
        self._tare_command_pending = False
        if self._tare_result is not True:
            if self._tare_result is False:
                diagnostic = self._tare_diagnostic
                detail = f' ({self._tare_reason})' if self._tare_reason else ''
                if diagnostic is not None:
                    detail += (
                        f"; samples={diagnostic['sample_count']}"
                        f" spread={diagnostic['spread_grams']:.3f}g"
                        f" ({diagnostic['spread_counts']} counts)"
                        f" limit={diagnostic['stability_limit_grams']:.3f}g"
                    )
                raise RuntimeError(f'HX711 firmware rejected tare{detail}')
            raise RuntimeError(
                'HX711 tare acknowledgement timed out; tare success was not assumed; '
                'check sensor readiness and competing serial readers'
            )
        if (self._tare_ack_uptime is None or self._tare_ack_monotonic is None
                or self._tare_ack_datetime is None):
            raise RuntimeError('HX711 successful tare acknowledgement lacked a valid uptime; tare was not accepted')
        self._anchor_uptime = self._tare_ack_uptime
        self._anchor_monotonic = self._tare_ack_monotonic
        self._anchor_datetime = self._tare_ack_datetime
        self._reset_measurement_state(preserve_clock_anchor=True)
        self._ready = True
        self._sensor_ready = True
        self._sensor_error = None
        self._needs_tare = False
        self._tare_result = None

    def close(self) -> None:
        self._closed = True
        connection, self._serial = self._serial, None
        self._reset_measurement_state()
        if connection is not None:
            try:
                connection.close()
            except Exception as error:
                self._log_serial_failure('close', error)
                raise RuntimeError(
                    f'HX711 serial close failed ({self._exception_summary(error)})'
                ) from error

def create_weight_source(source_type: str = "simulated", **kwargs) -> WeightSource:
    """
    Factory for constructing WeightSource implementations.

    Supported types:
    - 'simulated' (default): in-memory SimulatedWeightSource
    - 'stdin': StdinSimulatedWeightSource for interactive manual input
    - 'serial': SerialWeightSource (requires an explicit serial port)
    """
    if source_type == "simulated":
        return SimulatedWeightSource(
            initial_weight=kwargs.get("initial_weight", 500.0),
            clock=kwargs.get("clock", datetime.now),
        )
    elif source_type == "stdin":
        return StdinSimulatedWeightSource(
            initial_weight=kwargs.get("initial_weight", 500.0),
            clock=kwargs.get("clock", datetime.now),
        )
    elif source_type == "serial":
        return SerialWeightSource(
            port=kwargs.get("port"),
            baudrate=kwargs.get("baudrate", SerialWeightSource.BAUDRATE),
            timeout=kwargs.get("timeout", SerialWeightSource.SERIAL_TIMEOUT),
            max_weight_grams=kwargs.get("max_weight_grams", 20_000.0),
            clock=kwargs.get("clock", datetime.now),
            monotonic=kwargs.get("monotonic", time.monotonic),
            serial_factory=kwargs.get("serial_factory"),
        )
    else:
        raise ValueError(f"Unknown weight source type: {source_type!r}")
