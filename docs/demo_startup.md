# SmartCart — Software Demo Startup & Lifecycle Guide

## Overview

This document describes the software integration demo environment for SmartCart prior to physical load-cell/HX711 hardware integration.

> [!WARNING]
> **FakeProvider is for development/demo only and does not process real payments.**
> No real Razorpay credentials, keys, or networks are contacted.

> [!IMPORTANT]
> **Physical load-cell/HX711 input is not implemented in this milestone.**
> Weight events continue to use the simulated weight pipeline (`SimulatedWeightSource` / `StdinSimulatedWeightSource`). The `SerialWeightSource` boundary is prepared but intentionally unconfigured until physical hardware is connected in Stage 11.

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
# Step 1: Prepare the isolated demo database (never overwrites canonical smartcart.db)
python3 -m edge.demo.prepare_demo_db --clean

# Step 2: Run the demo composition with API server and background supervisor
python3 -m edge.demo.run_demo --cart-id CART-001 --prepare-db --with-supervisor
```

Or run individual demo components separately:

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
| Load Cell + HX711 | **NOT IMPLEMENTED** | Physical sensor wiring |
| Arduino Mega Firmware | **NOT IMPLEMENTED** | Calibration and USB serial output |
| Serial Protocol | **NOT IMPLEMENTED** | Fixed framing over USB CDC |
| Sensor Calibration | **NOT IMPLEMENTED** | Physical tare and slope calibration |
| Weight Thresholds | 10 g baseline preserved | Hardware-specific filtering |
| `SerialWeightSource` | Structural shell only (`NotImplementedError`) | Full serial reader emitting `WeightEvent` |
| `SimulatedWeightSource` | Active for software testing | Deprecated in physical testing |

---

## 5. Scope Boundaries

This milestone does **NOT** provide:
- Production readiness or real payment processing
- Real Razorpay credentials or network integration
- Retrained YOLO models or finalized product catalogs
- Physical load-cell or HX711 driver validation
- Theft / anomaly detection systems
