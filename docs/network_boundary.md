# SmartCart Pairing API — Network Boundary

## Edge API server host and port

The pairing API runs on the edge device (Raspberry Pi or development machine).
Default bind address and port:

| Variable              | Default    | Description                                      |
|-----------------------|------------|--------------------------------------------------|
| `SMARTCART_API_HOST`  | `0.0.0.0`  | Interface to bind. `0.0.0.0` = all interfaces.   |
| `SMARTCART_API_PORT`  | `5000`     | TCP port.                                        |

`0.0.0.0` means the server accepts connections on **all network interfaces**
of the edge device, including its LAN/Wi-Fi interface.  Alternatively, bind to the specific reachable LAN interface.

To restrict to local-only (development): set `SMARTCART_API_HOST=127.0.0.1`.

---

## Why `localhost` on Android does NOT reach the edge device

On an Android phone, `localhost` and `127.0.0.1` both refer to the **phone
itself** (the Android loopback interface), not to the Mac or Raspberry Pi.

This is a fundamental networking concept: each device has its own loopback
(`127.0.0.1`).  A URL like `http://localhost:5000` on Android means "connect
to port 5000 on this phone", which has nothing running there.

**Correct approach:** Use the edge device's **LAN IP address**.

---

## How to find the edge device LAN IP

On the edge device (Mac during development):

```bash
# macOS
ipconfig getifaddr en0       # Wi-Fi
ipconfig getifaddr en1       # Ethernet (if connected)
ifconfig | grep "inet "      # all interfaces
```

On Raspberry Pi:
```bash
hostname -I
```

The IP address will be in the form `192.168.x.x` or `10.x.x.x` depending on
the local router.

Example: if the edge device's LAN IP is `192.168.1.42`, the base URL is:

```
http://192.168.1.42:5000
```

---

## Network requirement: same local network

The Android phone and the edge device **must be on the same local network**:

- Same Wi-Fi access point, **or**
- Wired LAN that bridges to the same subnet.

If the phone is on a different network (e.g., mobile data), the edge device
LAN IP is not reachable without a VPN or port-forwarding configuration.
That is out of scope for this milestone.

---

## How the base URL must be supplied to Flutter

The Flutter app must receive the edge API base URL at runtime or build time.
It must **not** be hardcoded as a production constant in the Flutter source.

Options (in increasing order of robustness):

### Option A — Build-time `--dart-define` (simplest, for development)
```bash
flutter run --dart-define=SMARTCART_API_BASE_URL=http://192.168.1.42:5000
```
Accessed in Dart with:
```dart
const baseUrl = String.fromEnvironment(
  "SMARTCART_API_BASE_URL",
);
```

### Option B — Remote Config / compile-time flavor
Use Firebase Remote Config or Flutter build flavors to supply the URL per
environment (dev / staging / prod) without source changes.

### Option C — Dynamic discovery (future milestone)
The edge device announces its IP via mDNS or a QR code at cart startup.
Flutter reads the announced IP. This removes the need for manual configuration.

---

## Android emulator vs real device

| Client                          | Correct base URL                |
|----------------------------------|---------------------------------|
| Android emulator (AVD)          | `http://10.0.2.2:5000`         |
| Real Android phone (same Wi-Fi) | `http://<edge-device-ip>:5000` |
| Real Android phone (different network) | Not reachable without VPN |

`10.0.2.2` is the AVD's alias for the host machine's loopback (`localhost`
on the Mac).

---

## Security boundary

The edge API is designed for **same-network, on-premises** access.
It is not designed for public internet exposure.

- No TLS in this milestone (adding TLS is a future hardening step).
- Plain HTTP does not protect Bearer tokens from interception. Use only an
  isolated development network for HTTP; real deployment requires a protected
  transport such as HTTPS or a trusted encrypted network. TLS deployment is
  outside this task.
- Do not expose port 5000 to the internet through NAT/router port-forwarding.

---

## Summary checklist before Flutter integration

- [ ] Edge device is running `python -m edge.api.run_server`
- [ ] `SMARTCART_FIREBASE_URL` env var is set
- [ ] `GOOGLE_APPLICATION_CREDENTIALS` env var points to service-account JSON
- [ ] Phone and edge device are on the same Wi-Fi
- [ ] Edge device LAN IP is known (e.g. `192.168.1.42`)
- [ ] Flutter base URL is configured to `http://192.168.1.42:5000`
- [ ] `POST /pair-cart` has been manually tested with a real Firebase ID token

## Runtime startup

Install `requirements.txt` (includes Waitress), then from the repository root:

```sh
export SMARTCART_FIREBASE_URL="https://<approved-database-instance>"
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/service-account.json"
export SMARTCART_API_HOST="0.0.0.0"
export SMARTCART_API_PORT="5000"
python -m edge.api.run_server
```

These are placeholders, not credentials. Keep the credential file out of Git.
Startup validates host/port, calls the existing Firebase initializer, initializes
the existing SQLite schema at `data/smartcart.db`, constructs `create_app()`,
then starts Waitress with four threads and traceback exposure disabled. No
Flask development debugger/reloader is used; importing the module does not
initialize Firebase or bind a port. Invalid configuration/startup errors abort
before serving, without logging raw Firebase exception details. No CORS is
added: native Android does not require browser CORS permissions.

The phone must have a route to the configured edge host (normally the same
LAN), and the firewall must permit the configured port. `0.0.0.0` is a bind
address, not a client URL. Supply a required deployment-specific base URL to
future Flutter configuration; reject missing configuration instead of falling
back to localhost. No Flutter change or network discovery is implemented here.
