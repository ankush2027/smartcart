# Arduino Mega 2560 + HX711 serial input

This is the first physical-weight integration milestone. It adds a `WeightSource`
adapter and Arduino sketch; it does not alter billing, Firebase, pairing,
checkout, Flutter, or cart-session lifecycle code. Automated tests use fake
serial devices. The firmware has not been flashed or verified on hardware.

## Components and dependencies

- Arduino Mega 2560, HX711 module, 20 kg four-wire single-bar load cell.
- USB data cable between Mega and the MacBook.
- Arduino IDE plus the `bogde/HX711` library, installed manually.
- Python `pyserial` 3.5 or newer, declared in `requirements.txt`. Install project
  dependencies manually in the selected Python environment; this change does
  not install packages.
- A calibrated `data/model_info/best.pt` artifact and active SQLite product
  catalog matching the model classes are still required for the camera runtime.

Use the load-cell manufacturer's documentation to identify E+, E-, A+, A-.
Wire colors vary and are not used as a pinout assumption in this project.

## Wiring and protocol

Firmware is `firmware/smartcart_hx711/smartcart_hx711.ino`. Default Mega pins:

- HX711 DT/DOUT → Mega digital pin 3 (`HX711_DOUT_PIN`)
- HX711 SCK/CLK → Mega digital pin 2 (`HX711_SCK_PIN`)

The pins are constants and can be changed in the sketch. Confirm the HX711
module's supply and logic requirements against its documentation before
connecting it to the Mega. Do not connect/disconnect load-cell wiring while
powered.

USB serial runs at **115200 baud** and emits UTF-8 JSON Lines. Valid measurement
records have this shape:

```json
{"type":"weight","grams":125.4,"raw":123456,"stable":true,"sequence":12,"uptime_ms":1200,"calibrated":true}
```

`grams` is net load after tare; `raw` is raw averaged count. `sequence` and
`uptime_ms` are 32-bit unsigned counters. `stable` requires the last three
measurements to fit within a 2 g span (initial settings, to be tuned). Firmware
error and tare-ack records are status only, never measurements. The host rejects
malformed, uncalibrated, unstable, stale/out-of-order, negative, or over-capacity
records. It never converts a timeout or disconnect into zero. On reconnect, it
requires a new stable baseline before generating another delta, avoiding a
false event for load changes that happened while disconnected.

The host uses the latest stable sample as a baseline and emits the existing
`WeightEvent` when a subsequent stable reading differs by at least the current
10 g event threshold. ADD is a positive delta, REMOVE is negative. The timestamp
is reconstructed from MCU uptime anchored to the host's first received sample.
Existing event correlation still requires movement and weight within one second;
actual sample stability/latency must be checked on hardware before billing.

## Calibration and tare

The sketch intentionally refuses to emit measurements until a real calibration
factor is compiled in and the operator has tared the empty platform.

1. Mount the load cell and HX711 as intended. Identify wires from the maker's
datasheet; ensure the platform is empty and mechanically unloaded except for its
own tare weight.
2. Use the `bogde/HX711` library calibration example/API with the same DOUT/SCK
pins. With an empty platform, tare; place a known mass; compute
`(raw_loaded - raw_empty) / known_mass_grams`. Use a known mass verified on a
separate scale. The factor can be negative depending on mechanical orientation.
3. Verify several known masses across the useful range. Adding mass must
increase reported grams, removal must decrease it, and repeat measurements
must settle within a suitable noise range. Do not use the 20 kg rating itself
as a calibration weight.
4. Replace the default unconfigured macro state in the sketch with a numeric
`SMARTCART_CALIBRATION_FACTOR` value (raw counts per gram), compile and upload
manually using Arduino IDE. Keep the measured value with hardware notes; it is
specific to the mounted load cell.
5. With the cart platform empty, run the isolated Python sensor test below with
`--tare-empty`. Type `TARE` at its prompt. The firmware does not auto-tare at
power-up and cannot determine whether a product is on the platform.
6. Check stable/noisy behavior, known-mass accuracy, sequence freshness,
reconnection and event timing before connecting the source to a billing session.

## Isolated serial sensor test

This test opens only the serial port. It does not start a backend or camera and
does not import Firebase or SQLite code.

```sh
python scripts/test_hx711_serial.py --port /dev/cu.usbmodemXXXX
```

For the macOS device name, inspect `/dev/cu.*` or use
`python -m serial.tools.list_ports -v` after manually installing pyserial. Replace
the example path with the actual board port. To tare, only after verifying the
platform is empty:

```sh
python scripts/test_hx711_serial.py --port /dev/cu.usbmodemXXXX --tare-empty
```

Ctrl-C stops the isolated reader. The script prints measurements/events and
closes its serial connection.

## Select physical input in the existing runtime

The existing runtime still requires a pre-existing active session for the
selected cart, the same configured SQLite database as the pairing backend,
Firebase Admin configuration, an active catalog matching YOLO classes, and a
real model file. The serial source is explicitly selected and never falls back
to simulated input:

```sh
python -m edge.runtime.run_cart \
  --cart-id CART-001 \
  --model /absolute/path/to/best.pt \
  --camera-index 0 \
  --serial-port /dev/cu.usbmodemXXXX \
  --serial-baudrate 115200 \
  --serial-timeout 0.1 \
  --tare-empty
```

Use the repository's approved Python environment and Firebase/database
configuration before running. `run_cart` initializes the database schema at
startup, so confirm it points to the intended existing database first. The
physical serial path has not been run in this milestone; the command above is a
usage template, not a claim of end-to-end hardware readiness.

For software-only operation, the existing explicit flag remains:

```sh
python -m edge.runtime.run_cart --cart-id CART-001 --simulate-weight
```

`--simulate-weight` and `--serial-port` are mutually exclusive. One is required.
The current runtime requires `--model` vocabulary to exactly match the active
catalog and retains the existing product resolution and billing behavior.

For the explicitly selected two-product physical demo, startup can opt into an
in-memory class allowlist without changing active product rows. Only the exact
`Lays` and `Lifebuoy` model classes are eligible for resolution and billing;
other model classes remain detectable but have no resolver catalog entry and
cannot be billed. This does not weaken normal startup validation when the
option is omitted. Point BOTH the API and runtime at the same temporary copy of
the intended demo database. Before using the existing Firebase RTDB, verify
that the copied database's session IDs cannot collide with records already in
that RTDB. Independent SQLite databases must not write to the same
`/sessions/{session_id}` Firebase namespace. The current code does not add a
database namespace prefix, so use a dedicated RTDB instance/project for the
temporary database if session-ID uniqueness cannot be established.

After creating the temporary database copy and pairing an eligible customer to
an allowed cart through the authenticated Flutter flow, the physical runtime
command is:

```sh
python -m edge.runtime.run_cart \
  --cart-id CART-002 \
  --model /Users/Ankush/Desktop/smart-cart/data/model_info/best.pt \
  --camera-index 0 \
  --serial-port /dev/cu.usbmodemXXXX \
  --serial-baudrate 115200 \
  --serial-timeout 0.1 \
  --tare-empty \
  --demo-classes Lays,Lifebuoy
```

The 10 g sensor event threshold and ±15% resolver tolerance are unchanged.
Confirm each packaged product falls inside its catalog weight range before
billing; this option does not calibrate or approve product weights.
