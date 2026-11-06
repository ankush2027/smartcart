#!/usr/bin/env python3
"""Raw HX711 serial capture — hex bytes, timestamps, and newline positions.

Opens the serial port, optionally calls reset_input_buffer(), and reads raw
bytes for a configurable duration without attempting any JSON parsing.  Every
byte is logged in hex with a monotonic timestamp so the exact byte sequence and
inter-arrival gaps can be inspected offline.

This script does NOT start the cart runtime, does NOT write to SQLite, does NOT
send READY? or TARE, and does NOT replicate production code.  It exists
exclusively to observe what the OS delivers after port open.

Usage examples
--------------
# Capture 10 s of raw bytes, calling reset_input_buffer() first (default):
python scripts/capture_raw_serial.py --port /dev/cu.usbmodem141201 --duration 10

# Capture WITHOUT the flush to compare:
python scripts/capture_raw_serial.py --port /dev/cu.usbmodem141201 --duration 10 --no-flush

# Save to a file for offline analysis:
python scripts/capture_raw_serial.py --port /dev/cu.usbmodem141201 --duration 10 \
    --output /tmp/hx711_raw.txt

Analysis notes
--------------
Look for:
  - Lines shorter than ~80 bytes that contain only a JSON suffix (e.g. '28071}')
  - Gaps > 5 ms between bytes that should be part of the same record
  - Whether flush causes post-flush lines to start mid-record
  - Whether readline() would assemble fragments that in_waiting does not
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _open_port(port: str, baudrate: int, timeout: float):
    try:
        import serial
    except ImportError:
        sys.exit('pyserial is required; install declared requirements')
    return serial.Serial(port=port, baudrate=baudrate, timeout=timeout, exclusive=True)


def capture(port: str, baudrate: int, duration: float, flush: bool,
            read_timeout: float, output=None) -> None:
    t0 = time.monotonic()
    print(f'Opening {port} at {baudrate} baud  (flush={flush})')
    ser = _open_port(port, baudrate, read_timeout)
    t_open = time.monotonic()
    print(f'Port opened at T+{(t_open - t0)*1000:.1f} ms')

    if flush:
        fn = getattr(ser, 'reset_input_buffer', None)
        if callable(fn):
            fn()
            t_flush = time.monotonic()
            print(f'reset_input_buffer() called at T+{(t_flush - t0)*1000:.1f} ms')
        else:
            print('WARNING: reset_input_buffer() not available on this serial object')

    print(f'Capturing {duration} s of raw bytes.  Ctrl-C to stop early.')
    print()

    lines_out = []

    def emit(text: str) -> None:
        print(text)
        if output:
            lines_out.append(text + '\n')

    emit(f'{"T_ms":>10}  {"n_bytes":>7}  {"hex_bytes"}')
    emit('-' * 72)

    byte_buf = bytearray()
    line_count = 0
    total_bytes = 0
    t_deadline = time.monotonic() + duration

    try:
        while time.monotonic() < t_deadline:
            waiting = getattr(ser, 'in_waiting', None)
            if waiting is not None and waiting > 0:
                chunk = ser.read(waiting)
            else:
                chunk = ser.read(1)  # blocking read, up to read_timeout
            if not chunk:
                continue
            t_now = time.monotonic()
            total_bytes += len(chunk)
            byte_buf.extend(chunk)
            # Process complete lines (terminated by \n)
            while b'\n' in byte_buf:
                nl_pos = byte_buf.index(b'\n')
                line_bytes = bytes(byte_buf[:nl_pos + 1])  # include \n
                del byte_buf[:nl_pos + 1]
                line_count += 1
                rel_ms = (t_now - t0) * 1000
                hex_str = line_bytes.hex(' ')
                # Flag lines that are suspiciously short (likely fragments)
                # A complete status/error record is at least 70 bytes.
                flag = ' *** FRAGMENT?' if len(line_bytes) < 70 else ''
                emit(f'{rel_ms:10.1f}  {len(line_bytes):7d}  {hex_str[:120]}{flag}')
                # Also emit the decoded text for readability
                try:
                    decoded = line_bytes.rstrip(b'\r\n').decode('utf-8')
                    emit(f'{"":10}  {"":7}  TEXT: {decoded[:120]}')
                except UnicodeDecodeError:
                    emit(f'{"":10}  {"":7}  (non-UTF-8 bytes)')
    except KeyboardInterrupt:
        print('\nCapture stopped by user.')
    finally:
        ser.close()

    t_end = time.monotonic()
    elapsed = t_end - t0
    print()
    print(f'--- SUMMARY ---')
    print(f'Duration:    {elapsed:.2f} s')
    print(f'Total bytes: {total_bytes}')
    print(f'Lines seen:  {line_count}')
    print(f'Partial buf: {len(byte_buf)} bytes remaining (no trailing \\n)')
    if byte_buf:
        print(f'Partial:     {bytes(byte_buf).hex(" ")}')

    if output:
        Path(output).write_text(''.join(lines_out))
        print(f'Output written to: {output}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', required=True,
                        help='Serial device, e.g. /dev/cu.usbmodem141201')
    parser.add_argument('--baudrate', type=int, default=115200)
    parser.add_argument('--duration', type=float, default=10.0,
                        help='Seconds to capture (default 10)')
    parser.add_argument('--no-flush', action='store_true',
                        help='Do NOT call reset_input_buffer() after port open')
    parser.add_argument('--read-timeout', type=float, default=0.1,
                        help='Per-read timeout in seconds (default 0.1)')
    parser.add_argument('--output', metavar='FILE',
                        help='Save output to FILE in addition to stdout')
    args = parser.parse_args(argv)
    capture(
        port=args.port,
        baudrate=args.baudrate,
        duration=args.duration,
        flush=not args.no_flush,
        read_timeout=args.read_timeout,
        output=args.output,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
