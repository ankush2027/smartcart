#!/usr/bin/env python3
"""Read an HX711 serial stream without starting SmartCart services or touching SQLite."""
import argparse
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge.runtime.weight_source import SerialWeightSource


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='USB serial device, e.g. /dev/cu.usbmodem...')
    parser.add_argument('--baudrate', type=int, default=SerialWeightSource.BAUDRATE)
    parser.add_argument('--timeout', type=float, default=SerialWeightSource.SERIAL_TIMEOUT)
    parser.add_argument('--tare-empty', action='store_true',
                        help='send TARE after requiring explicit typed confirmation')
    args = parser.parse_args(argv)
    source = SerialWeightSource(args.port, args.baudrate, args.timeout)
    try:
        if args.tare_empty:
            confirmation = input('Confirm the cart platform is physically empty; type TARE to continue: ')
            if confirmation != 'TARE':
                print('Tare cancelled; no command sent.')
                return 2
            source.tare()
            print('TARE command sent; waiting for stable sensor readings.')
        last_sequence = None
        print('Reading sensor only. No camera, backend, Firebase, or database is used. Ctrl-C stops.')
        while True:
            event = source.poll()
            reading = source.latest_reading
            if reading is not None and reading.sequence != last_sequence:
                last_sequence = reading.sequence
                print(f"sequence={reading.sequence} grams={reading.grams:.1f} "
                      f"raw={reading.raw} stable={reading.stable}")
            if event is not None:
                print(f"WeightEvent {event.event_type}: {event.weight_change:+.1f} g "
                      f"at {event.timestamp.isoformat()}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        print('Sensor read stopped.')
        return 0
    finally:
        source.close()


if __name__ == '__main__':
    raise SystemExit(main())
