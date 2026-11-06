# SmartCart HX711 serial firmware

Target: Arduino Mega 2560 + HX711 + a 20 kg four-wire single-bar load cell.
The firmware uses the **bogde/HX711** Arduino library (`HX711.h`). Install
that library manually through Arduino IDE Library Manager; this repository
never installs or flashes firmware automatically.

## Wiring

Default Mega digital pins are `HX711_DOUT_PIN = 3` (HX711 DT/DOUT) and
`HX711_SCK_PIN = 2` (HX711 SCK/CLK); both are constants in the sketch and may
be changed to match the physical build. Connect the HX711 board to the Mega
using the board's documented supply and logic requirements. Connect load-cell
excitation and signal conductors to E+, E-, A+, and A- only according to the
load-cell manufacturer's documentation or verified measurements. **Do not
infer wire function from wire color.** Confirm the Mega, HX711 module, load
cell wiring, mounting, and supply before powering the assembly.

## Calibration and tare

The sketch intentionally has no guessed calibration factor. Without a
compile-time `SMARTCART_CALIBRATION_FACTOR`, it reports
`{"type":"error","code":"calibration_required",...}` and emits no weight
samples. Determine the factor for the actual mounted load cell using a known
mass and the bogde/HX711 library's calibration example/API: with the platform
empty, tare; place a known mass in grams; calculate the raw-count change per
gram (`(raw_loaded - raw_empty) / known_mass_grams`). The factor may be
negative if the sensor orientation makes raw counts decrease as load
increases. Verify the sign by adding/removing a known mass and ensure the
reported grams increase with load. Set the measured numeric result as
`#define SMARTCART_CALIBRATION_FACTOR <measured-counts-per-gram>` near the
sketch constants (replace the placeholder with a valid numeric literal), then
compile and upload through Arduino IDE. Repeat with several known masses
across the intended range. Do not use the 20 kg rating as a calibration mass.

At each power-up the sketch requires a `TARE` command; it does not auto-zero a
possibly loaded cart. Empty the cart platform, then use the isolated Python
sensor tool's `--tare-empty` option. The HX711 acknowledges the tare and begins
stable readings. Tare is an operator action; firmware cannot establish that
the platform is empty. Never tare with products or other load on the platform.

`STABILITY_SAMPLE_COUNT` (3) and `STABILITY_RANGE_GRAMS` (2 g) are initial
engineering settings, not validated physical calibration. Verify noise and
settling on the actual mount. Existing backend event correlation is limited to
one second, so validate that stable reports arrive within that window before
billing trials.

After tare, calculated readings from -2 g through +2 g are reported as zero.
This bounded deadband covers the observed empty-platform fluctuations; readings
below -2 g and readings above the 20 kg capacity remain errors. It does not
change tare behavior or the existing stability test.

## Serial contract

115200 baud, USB CDC, UTF-8 JSON Lines (one JSON object followed by `\n`).
All records carry integer `protocol_version: 1`. The sketch emits a startup
`status/ready` record after HX711 pin initialization and repeats it every
second. Because opening the Mega's serial port can reset it, the host also
sends `READY? <uint32-request-id>\n`. The firmware replies with a ready record
that echoes the request ID; only that matching versioned reply completes the
host readiness handshake. A startup heartbeat or `tare_required` alone does
not authorize TARE.

Weight record (emitted only after a configured calibration, successful tare,
and valid sensor conversion):

```json
{"type":"weight","protocol_version":1,"grams":125.4,"raw":123456,"stable":true,"sequence":12,"uptime_ms":1200,"calibrated":true}
```

`sequence` and `uptime_ms` are unsigned 32-bit counters and wrap naturally.
`grams` is net load in grams after tare; `raw` is the averaged HX711 raw count.
`stable` means the last three readings span no more than the configured range.

Firmware sensor status records use `type: "error"` with a short code, including
`tare_required`, `calibration_required`, `hx711_not_ready`, or
`weight_out_of_range`; they are not measurements and Python never turns them
into zero. Tare acknowledgement is `{"type":"tare","ok":true,...}` and
includes the sample count and measured stability-window spread. The host sends
`TARE\n` only after the matching readiness response and sensor-ready evidence;
unknown commands are rejected.
