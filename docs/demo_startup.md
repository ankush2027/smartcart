# SmartCart — Software Demo Startup & Lifecycle Guide

## Overview

This document describes the software integration demo environment for SmartCart prior to physical load-cell/HX711 hardware integration.

> [!WARNING]
> **FakeProvider is for development/demo only and does not process real payments.**
> No real Razorpay credentials, keys, or networks are contacted.

> [!IMPORTANT]
> **The Stage 10 software demo remains simulated-weight only.**
> A separate physical HX711 serial adapter is now available for the manual cart runtime; it requires the documented Arduino firmware, a calibrated/tared sensor, and an explicit `--serial-port`. See `docs/hx711_serial_integration.md`. No physical hardware test is implied by the software demo.

---

## 1. Startup Modes

### A. Normal Startup (Production-like Default)

In normal startup, the backend starts without `PaymentService` and **never** silently falls back to `FakeProvider`:

```bash
# 1. Start pairing & checkout API server (Payment disabled)
python3 -m edge.api.run_server

# 2. In a separate terminal, start the manual cart runtime
python3 -m edge.runtime.run_cart --cart-id CART-001 --simulate-weight
```

In this mode:
- Payment endpoints (`/payment`) return HTTP `503 payment_unavailable`.
- The cart runtime requires an existing active session created via `/pair-cart`.

### B. Explicit Software-Demo Startup

Demo mode requires the same valid backend Firebase configuration as normal startup:
`SMARTCART_FIREBASE_URL` and `GOOGLE_APPLICATION_CREDENTIALS` (the path to a
service-account file kept outside source control). Do not put Admin credentials
in Flutter. Firebase Admin and the RTDB root reference are initialized before
database preparation, runtime workers, or HTTP serving. Invalid or missing
configuration stops startup with a sanitized error; it never disables authentication.

Protected requests still require the customer's Firebase ID token. The supervised
runtime passes the initialized RTDB root to `CartSession`, so successful ADD/REMOVE
mutations use the existing post-commit Firebase synchronization path. A Firebase
write failure does not undo SQLite billing. Demo payment completion uses the same
root for its existing reconciliation hook; a projection failure does not undo
verified payment completion. FakeProvider remains explicit demo-only.

The explicit demo composition wires `PaymentStore`, `FakeProvider`, and `PaymentService` with a completion projection hook, while operating on an **isolated demo database** (`data/smartcart_demo.db`):

```bash
# Step 1: Prepare the isolated demo database without resetting existing sessions
python3 -m edge.demo.prepare_demo_db

# Step 2: Run the demo composition with API server and background supervisor
python3 -m edge.demo.run_demo --cart-id CART-001 --prepare-db --with-supervisor --software-input --reconcile-demo-projections
```

Or run individual demo components separately:

The following separate runtime uses a camera/YOLO and the original stdin weight
source (which uses `select` and is unsuitable for Windows console input). For the
deterministic Windows software-only demo use the managed command above instead.

```bash
# Terminal 1: Run demo HTTP API with FakeProvider enabled
export SMARTCART_DATABASE_PATH=data/smartcart_demo.db
python3 -m edge.demo.run_demo --cart-id CART-001

# Terminal 2: Run interactive supervisor or cart runtime
export SMARTCART_DATABASE_PATH=data/smartcart_demo.db
python3 -m edge.runtime.run_cart --cart-id CART-001 --simulate-weight
```

---

## 2. Managed Cart Runtime Lifecycle (`CartSupervisor`)

The `CartSupervisor` (`edge/runtime/supervisor.py`) manages the full runtime lifecycle for a cart without requiring manual application restarts between customers:

```
[IDLE]
  │ (No active session found — supervisor polls safely)
  ▼
[Customer Pairs CART-001]
  │ (Supervisor discovers active session)
  ▼
[RUNNING] (Fresh CartRuntime attached)
  │ Camera + MOG2 + Simulated Weight -> EventOrchestrator -> YOLO -> Resolver -> Billing -> Firebase
  │
  ├──► [Checkout Frozen]
  │       │ (Billing mutations blocked; runtime drains and releases resources)
  │       ▼
  │     [DRAINING]
  │       │
  │       ├── If Cancelled ──► [IDLE] ──► Attaches fresh CartRuntime (hydrates new generation)
  │       │
  │       └── If Paid ──────► [COMPLETED] ──► [IDLE] (Session ended, waits for next customer)
  │
  ▼
[Customer B Pairs CART-001]
  │ (New session discovered automatically)
  ▼
[RUNNING] (Fresh CartRuntime attached for Customer B; no state leaked)
```

### Key Invariants
- **SQLite Authority**: SQLite is the authoritative source of truth.
- **Generational Fencing**: When checkout is cancelled, the old runtime is fenced out. Any attempt by an old runtime to mutate billing raises `StaleRuntimeError`.
- **Per-Cart Process Locking**: Guaranteed by `cart_lock` (file flock/msvcrt lock). Two runtimes cannot attach to the same cart simultaneously.
- **Resource Ownership**: Camera and weight source threads are deterministically stopped and joined before attaching any replacement runtime.

---

## 3. Controlled Demo Payment Simulation

During the demo, payment is completed by exercising the full verification and completion pipeline using authentic HMAC signatures from `FakeProvider`:

```bash
# Example: Trigger fake payment completion via demo helper endpoint
curl -X POST http://localhost:5000/demo/sessions/<SESSION_ID>/checkouts/<CHECKOUT_ID>/simulate-payment-success \
  -H "Authorization: Bearer <CUSTOMER_FIREBASE_TOKEN>"
```

This helper:
1. Verifies customer ownership.
2. Retrieves the provider order from the active payment attempt.
3. Records a captured payment on `FakeLedger`.
4. Computes valid HMAC evidence.
5. Calls `PaymentService.submit_evidence(...)` which performs `verify()`, `save_verified()`, `complete()`, and fires the Firebase completion projection hook.

---

## 4. Physical Weight Sensor Boundary

| Component | Status in this Milestone | Planned Next Milestone (Stage 11) |
|---|---|---|
| Load Cell + HX711 | Hardware wiring/calibration not verified | Confirm manufacturer wiring and follow `docs/hx711_serial_integration.md` |
| Arduino Mega Firmware | Added under `firmware/smartcart_hx711/`; not flashed/tested on board | Manual library installation, compile, and physical validation |
| Serial Protocol | JSON Lines at 115200 baud implemented in firmware and Python adapter | Validate on the actual USB device |
| Sensor Calibration | Firmware requires configured factor and explicit empty-platform tare | Calibrate against known masses on the mounted load cell |
| Weight Thresholds | Existing 10 g software event threshold; firmware stability defaults 2 g over 3 readings | Validate settling against existing one-second movement correlation |
| `SerialWeightSource` | Implemented; fake-serial tests only | Verify USB reconnection and real HX711 readings |
| `SimulatedWeightSource` | Active for software testing | Deprecated in physical testing |

---

## 5. Scope Boundaries

This milestone does **NOT** provide:
- Production readiness or real payment processing
- Real Razorpay credentials or network integration
- Retrained YOLO models or finalized product catalogs
- Physical load-cell or HX711 driver validation
- Theft / anomaly detection systems

## 6. Controlled repair after a demo database reset

Flutter's owner-scoped query already excludes sessions with an `end_time`. It
cannot tell that an apparently active Firebase session has disappeared from
SQLite. `prepare_demo_db --clean` deletes the local database only; it does not
close remote sessions. Do not reset the database as a routine startup step.

`--reconcile-demo-projections` is explicit operator consent to reconcile the
selected cart from this database. Use a **dedicated demo Firebase namespace and
cart**, and stop **all API/runtime/recovery writers sharing that namespace** first.
Do not use an empty/wrong database to repair somebody else's cart.

Before HTTP serving or supervisor attachment, repair:

- Removes only active projections for `--cart-id` absent from SQLite.
- Preserves other carts and completed orphan history.
- Rebuilds valid local session metadata and bill; preserves their event logs.
- Rejects conflicting cart/owner/start-time identities, rather than rebinding an
  old session ID to a different purchase. Such collisions require operator review.
- Reserves SQLite session IDs above all observed remote session IDs, avoiding
  reuse after a clean/reset. This reservation changes no bill or payment rows.
- Uses a Firebase transaction and stops startup if remote state changes during
  repair or if the write fails. After an uncertain reply, retry only with writers
  still stopped; the operation is repeatable and the ID reservation remains safe.

This is an operator recovery procedure, not concurrency control between multiple
independent SQLite authorities. Do not share one RTDB session namespace between
independent active databases. It is not an API, client write, or auth bypass.
Completion projection reads the current configured database, even when the demo
runner selects it after Python imports.

## 7. Windows PowerShell software-only acceptance run

Stop existing backends first. Preserve the existing demo DB. In backend terminal:

```powershell
Set-Location 'C:\Users\Rakshan\OneDrive\Desktop\smartcart'
$env:SMARTCART_FIREBASE_URL = Read-Host 'Dedicated demo RTDB URL'
$env:GOOGLE_APPLICATION_CREDENTIALS = Read-Host 'Absolute service-account file path outside the repository'
if (-not (Test-Path -LiteralPath $env:GOOGLE_APPLICATION_CREDENTIALS -PathType Leaf)) { throw 'Credential path does not exist' }
$env:SMARTCART_ALLOWED_CARTS = 'CART-001'
$env:SMARTCART_DATABASE_PATH = Join-Path (Get-Location) 'data\smartcart_demo.db'

# The repository venv currently points to a missing Python installation in this
# environment. This bundled Python 3.12 + existing venv packages ran the tests.
$demoPython = 'C:\Users\Rakshan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = Join-Path (Get-Location) '.venv\Lib\site-packages'
& $demoPython -B -m edge.demo.run_demo --cart-id CART-001 --prepare-db --with-supervisor --software-input --reconcile-demo-projections
```

Do not print the service-account file or tokens. Ensure Firebase client setup in
Flutter points to the same project. The existing auth checks and RTDB rules must
remain enabled. No rules or credentials are deployed by these commands.

In another PowerShell terminal, with the phone on the same trusted development
LAN and USB debugging enabled:

```powershell
Set-Location 'C:\Users\Rakshan\.codex\worktrees\smartcart-flutter-e2e\smartcart\app'
$flutterSdk = Read-Host 'Flutter SDK folder containing bin\flutter.bat'
$flutter = Join-Path $flutterSdk 'bin\flutter.bat'
& $flutter analyze
& $flutter test
& $flutter devices
$phoneId = Read-Host 'Android device ID from flutter devices'
$backendIp = Read-Host 'Backend PC LAN IPv4 address (not localhost or 0.0.0.0)'
& $flutter run --debug -d $phoneId "--dart-define=SMARTCART_API_BASE_URL=http://${backendIp}:5000" --dart-define=SMARTCART_DEMO_PAYMENT=true
```

Debug builds already permit LAN HTTP; this sends authenticated requests over the
development LAN. Use a trusted isolated network; do not expose port 5000 publicly.
Firewall/network connectivity must permit phone-to-PC access. The demo payment
flag exposes the existing backend fake-payment button with **live** Firebase
cart data; it does not enable mock cart fixtures or supply credentials.

1. Sign in as Customer A. After repair, an orphan must no longer block the
   `Enter Cart ID` screen. A genuine active SQLite session is deliberately retained.
2. Pair `CART-001`. Wait for backend `DEMO INPUT session=<S> generation=<G>`.
   In the **running backend console**, type the following, replacing `S` and `G`
   with the printed integers (these are console commands, not PowerShell):

   ```text
   STATUS
   ADD S G "Lays"
   REMOVE S G "Lays"
   ADD S G "Lays"
   ```

   Expect committed ADD/REMOVE outcomes and Flutter totals ₹10 → ₹0 → ₹10.
   The command uses catalog weight and fixture vision/motion, then the existing
   WeightEvent → EventOrchestrator/MotionPipeline → ProductResolver/CartSession
   → SQLite → Firebase path. It tests no camera/MOG2/YOLO accuracy. Catalog and
   10g event threshold are unchanged; Bru (3.7g) is intentionally unsupported.
3. Tap Checkout, wait for supervisor drain, then Cancel Checkout. Wait for a
   **new generation** in the console. An ADD using the old generation must be
   rejected. Use the new generation to add `"Coca Cola"` (total now ₹20).
4. Checkout again → Pay Now → Complete Demo Payment. Only the backend's
   `COMPLETED` response may show Payment Successful. The receipt remains visible
   when the completion projection removes the active cart. Verify SQLite
   completion is reflected by RTDB `end_time` and the supervisor release log.
5. Logout A, login B, pair `CART-001`. Verify a new session ID, empty bill, new
   runtime, and no A items. A console command containing A's session ID must fail.

The console uses a single portable stdin reader and runtime-owned bounded queue;
queued commands never move to another runtime. It is only enabled by the explicit
demo flag. Normal backend startup and physical sensor code are unchanged.

If Firebase writes fail, SQLite mutations remain committed. Do not replay ADDs;
stop writers and reconcile before continuing. FakeProvider's ledger is in memory:
keep the backend running through a payment attempt. Restart recovery for an
unfinished fake order is not provided by this demo change. Live Firebase/phone
acceptance must be performed separately from offline test results.
