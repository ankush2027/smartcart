# SmartCart Pairing Endpoint

## Overview

`POST /pair-cart` is the trusted backend endpoint that pairs an authenticated
Firebase customer to an allowed physical shopping cart. It uses
`pair_and_assign_in_transaction` to create a fresh owned session when the cart
has no active session, assign a pre-created unowned session, or reconnect its owner.

SQLite is the authoritative source of truth.  Firebase is a downstream
synchronization layer.  The endpoint derives customer identity exclusively
from the verified Firebase ID token — **a client-supplied `customer_uid` field
in the request body is always ignored**.

---

## Authentication

Every request must supply a Firebase ID token in the `Authorization` header
using the standard Bearer scheme:

```
Authorization: Bearer <Firebase-ID-token>
```

The backend verifies the token using the Firebase Admin SDK
(`firebase_admin.auth.verify_id_token`).  The verified `uid` is used as the
customer identity.  Any other value in the request body that claims to be a
UID is silently ignored.

Tokens that are missing, malformed, expired, or revoked are rejected before
any database operation is performed.

---

## Request

```
POST /pair-cart
Content-Type: application/json
Authorization: Bearer <Firebase-ID-token>

{
    "cart_id": "<physical-cart-identifier>"
}
```

| Field     | Type   | Required | Description                                              |
|-----------|--------|----------|----------------------------------------------------------|
| `cart_id` | string | Yes      | Non-empty stable physical cart identifier (e.g. cart label). |

No other fields are required.  Extra fields are silently ignored (they cannot
escalate privileges because identity comes exclusively from the verified token).

## Allowed physical carts

The pairing backend reads the comma-separated `SMARTCART_ALLOWED_CARTS`
environment variable. When unset, only `CART-001` is allowed for this prototype.
Configure additional physical carts before starting the pairing API:

```powershell
$env:SMARTCART_ALLOWED_CARTS = 'CART-001,CART-002,CART-003'
python -m edge.api.run_server
```

On Linux/macOS, use `export SMARTCART_ALLOWED_CARTS='CART-001,CART-002,CART-003'`.
This value replaces the default. Surrounding whitespace is trimmed, duplicates
collapse, and empty comma-separated entries are ignored. An explicitly empty
or whitespace-only value allows no carts. IDs are case-sensitive; there are no
wildcards. Request IDs are also trimmed by the service. Unknown IDs return
`cart_not_allowed` (404), even if an old session for that ID exists.

Eligibility is checked inside the same SQLite writer transaction as ownership
and creation. Configuration is server-side and introduces no cart table or
schema migration. Keep the configured IDs aligned with deployed physical carts;
removing an ID prevents reconnect but does not complete or erase its session.

---

## Pairing flow

```
Customer authenticates with Firebase
        ↓
Flutter obtains Firebase ID token
        ↓
Customer supplies / selects physical cart_id
        ↓
Flutter sends:  POST /pair-cart
                Authorization: Bearer <Firebase-ID-token>
                { "cart_id": "..." }
        ↓
Backend extracts Bearer token
        ↓
firebase_admin.auth.verify_id_token(token)
        ↓
Backend extracts verified UID
        ↓
BEGIN IMMEDIATE; check configured cart eligibility
        ↓
Read active customer/cart sessions and validate uniqueness/ownership
        ↓
Create owned session / assign unowned session / reconnect same owner
        ↓
COMMIT SQLite transaction
        ↓
Firebase session synchronization for new creation/assignment
        ↓
HTTP 200 response
```

---

## Ownership invariants

### Cart invariant
- A `cart_id` must be configured as allowed and have at most one active session.
- A session is active when `end_time IS NULL`.
- Zero active sessions → create a fresh session, provided the customer has no
  other active session. Completed sessions are never reopened or reset.
- Two or more active sessions → `multiple_active_sessions_for_cart` (409).
  Backend reconciliation is required; the endpoint never silently selects one.

### Customer invariant
- A Firebase UID may own **at most one** active session.
- If the UID already owns the **same** session → idempotent success (`already_paired`).
- If the UID already owns a **different** active session → rejected with
  `customer_already_has_active_session` (409), including when the requested cart
  has no session. No orphan session is created.

### Session ownership
- If the target session has no `customer_uid` → assignment proceeds.
- If already assigned to the **same** UID → idempotent success.
- If assigned to a **different** UID → rejected with `cart_already_assigned` (409).
  The endpoint **never steals or reassigns** an owned session.

---

## Success response

HTTP `200 OK`.

```json
{
    "session_id": 42,
    "cart_id": "CART-001",
    "status": "paired"
}
```

| Field        | Type    | Description                                                         |
|--------------|---------|---------------------------------------------------------------------|
| `session_id` | integer | The SQLite session ID for the paired session.                       |
| `cart_id`    | string  | The physical cart identifier that was paired.                       |
| `status`     | string  | `"paired"` (new session or assignment) or `"already_paired"` (idempotent). |

The response intentionally **does not** include:
- `customer_uid` (the UID is already known to the client from their auth token)
- Firebase tokens or Admin SDK credentials
- Internal database details, total amounts, or session history

Flutter will subsequently obtain live billing state from RTDB using the
security rules (customer-uid-constrained query).

---

## Error responses

All errors return JSON with a stable machine-readable `error` code and a
human-readable `message` that contains no credentials or stack traces.

| HTTP Status | `error` code                              | Cause                                                      |
|-------------|-------------------------------------------|------------------------------------------------------------|
| 400         | `invalid_cart_id`                         | `cart_id` is missing, empty, or whitespace-only.           |
| 400         | `invalid_request`                         | Request body is not valid JSON.                            |
| 401         | `authentication_required`                 | `Authorization: Bearer` header is missing or malformed.    |
| 403         | `invalid_token`                           | Firebase ID token is invalid, expired, or revoked.         |
| 404         | `cart_not_allowed`                        | The physical cart ID is not enabled in backend configuration. |
| 409         | `cart_already_assigned`                   | The target session is already owned by a different customer. |
| 409         | `customer_already_has_active_session`     | This customer already owns a different active session.     |
| 409         | `multiple_active_sessions_for_cart`       | Data integrity conflict — multiple active sessions for this cart. |
| 409         | `multiple_active_sessions_for_customer`   | Data integrity conflict — customer owns multiple active sessions. |
| 500         | `internal_error`                          | Unexpected backend failure.                                |

### Example error response

```json
{
    "error": "cart_already_assigned",
    "message": "This cart is already assigned to another customer."
}
```

---

## SQLite authority

SQLite is authoritative.  All ownership reads and writes go to SQLite:

- `sessions.cart_id` — physical cart identifier (set at session creation).
- `sessions.customer_uid` — Firebase Auth UID (set by this endpoint).

The endpoint never consults Firebase RTDB for ownership decisions.  Firebase
is a read-only downstream view of SQLite state.

---

## Firebase synchronization behavior

After successful SQLite creation/assignment, the endpoint calls
`edge.firebase.sync.sync_session` with the full session metadata
(including `cart_id` and `customer_uid`) via the Firebase Admin SDK.

**Firebase failure does NOT roll back SQLite ownership.**  If sync fails:

- SQLite ownership is preserved (authoritative).
- The failure is logged with full context (session_id, customer_uid, cart_id).
- The endpoint still returns HTTP 200 because the assignment succeeded.
- Recovery is possible by calling `sync_session` again from SQLite data.

The Firebase RTDB path is `sessions/{session_id}/customer_uid`.  Once
synchronized, the Flutter client can discover the session via:

    /sessions.orderByChild("customer_uid").equalTo(auth.uid)

per the authorization contract in `docs/rtdb_authorization_contract.md`.

---

## No automatic unpair on logout

Customer logout from Flutter **does not** clear `customer_uid`.

A physical shopping session does not end when a customer closes the app.
The session lifecycle is managed by the backend/cart hardware
(`complete_session`).  Unpair/release flows are a separate future milestone.

## Creation, completion and reuse

New sessions use `billing.create_session(cart_id, customer_uid=verified_uid,
_conn=pairing_connection)`. The canonical API inserts a new auto-incremented ID,
the database timestamp, the cart and verified owner, a zero total, and a null
`end_time`. It creates no bill items or event history. An injected connection
must already have an active transaction; creation does not open another
connection, commit, roll back, or close the caller's connection. Standalone
`create_session()` calls retain their existing behavior and optional unowned
session creation.

Reconnect returns the existing ID without clearing any bill, total or history.
Canonical completion (`CartSession.complete()` → `billing.complete_session()`)
sets `end_time` and the final total, then the coordinator synchronizes metadata.
It retains the original cart, customer, timestamps, bill and event records.
Once completed, the next authorized customer pairing to the same allowed cart
gets a new session and a zero bill under a different Firebase session path.
No cart status field is needed.

Pair first, then start/resume the runtime against that active session. An
existing runtime remains bound to its original session; after completion it
requires controlled shutdown/restart before serving another session. No
automatic runtime turnover, payment or automatic Firebase repair is implemented.
The [checkout endpoint](checkout.md) freezes a nonempty bill without completion
or ownership release; owner reconnect still returns that same frozen session.
Future trusted checkout/payment orchestration must verify payment on the backend
and coordinate the runtime/final bill before calling canonical completion.
Flutter cannot declare payment success or complete/release sessions directly.

---

## Concurrency and transaction strategy

The endpoint verifies the Firebase token before calling
`pair_and_assign_in_transaction`. One connection executes `BEGIN IMMEDIATE`,
checks cart eligibility, reads active customer/cart sessions, rejects duplicates
and ownership conflicts, then creates, assigns or confirms the same owner and
commits once. Creation and customer checks share that transaction, so concurrent
requests cannot both create sessions for one cart or one customer. Conflicts
and SQL failures roll back, including an inserted session. Injected test
connections follow the same transaction protocol; pre-existing transactions
are rejected without committing or rolling back the caller's work.

SQLite serializes writers to the same local database file, including writers
in other processes. The default busy timeout is five seconds; lock exhaustion
becomes a safe internal-error response. No distributed or cross-database
uniqueness guarantee is claimed. Legacy assignment/session-creation helpers
can still violate invariants after a pairing transaction; this is not a global
schema constraint.

Firebase sync starts only after SQLite commit and connection release. Failure
preserves SQLite ownership and the existing success response. Same-owner retry
remains `already_paired` and currently does not retry failed Firebase sync; a
later backend synchronization must repair lag. Operators can use
[controlled projection reconciliation](controlled_reconciliation.md) after
quiescing relevant writers. Pairing does not invoke it. No outbox or automatic retry
queue is introduced. Concurrent post-commit backend updates can still cause
RTDB synchronization ordering issues; the transaction does not serialize
network writes.

Run the existing factory using `python -m edge.api.run_server` from the repo
root after installing requirements. This initializes the existing Firebase
client and SQLite schema before serving via Waitress (not Flask's development
server), with debugging disabled. See [network configuration](network_boundary.md).

---

## Example request (no real credentials)

```bash
# Replace <FIREBASE-ID-TOKEN> with a real token from Firebase Auth.
# Replace CART-001 with the physical cart label.

curl -X POST http://localhost:5000/pair-cart \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FIREBASE-ID-TOKEN>" \
  -d '{"cart_id": "CART-001"}'
```

Expected success response:

```json
{
    "session_id": 42,
    "cart_id": "CART-001",
    "status": "paired"
}
```

---

## Files

| File                             | Purpose                                             |
|----------------------------------|-----------------------------------------------------|
| `edge/api/server.py`             | Flask app factory; `POST /pair-cart` route.         |
| `edge/pairing/token_verifier.py` | Firebase ID-token verification abstraction.         |
| `edge/pairing/session_store.py`  | Active-session lookup helpers (SQLite).             |
| `edge/pairing/pair_cart.py`      | Core pairing logic (validation + assignment).       |
| `tests/test_pair_cart.py`        | Offline HTTP/security/pairing regression tests.     |
| `tests/test_pairing_lifecycle.py` | Creation, reconnect, reuse, races and failure boundaries. |
| `edge/billing/billing.py`        | Transaction-aware `create_session`, session metadata and completion. |
| `edge/firebase/sync.py`          | `sync_session` (includes cart_id, customer_uid).    |
| `configs/firebase_rules.json`    | RTDB authorization rules (not yet deployed).        |
