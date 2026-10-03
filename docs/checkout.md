# Checkout freeze — backend phase 1

SQLite is authoritative. The lifecycle is SHOPPING → CHECKOUT_FROZEN → future
VERIFIED_PAYMENT → COMPLETED. This milestone implements only SHOPPING →
CHECKOUT_FROZEN. No payment integration, cancellation, unfreeze, customer
completion endpoint, or automatic runtime turnover exists.

## State and deployment

`sessions.checkout_started_at TEXT` is nullable. Initialization inspects
`PRAGMA table_info(sessions)` and adds it only when missing; migration failure
propagates and prevents normal startup. Existing rows get NULL without invented
checkout history. Repeated initialization preserves existing values.

- Shopping: end_time and checkout_started_at are NULL.
- Frozen: end_time is NULL and checkout_started_at is non-NULL.
- Completed: end_time is non-NULL. Checkout timestamp remains historical.

Stop old writers before deployment. All billing writers must be upgraded
together and use the same authoritative database. Old binaries ignore the
freeze; running them concurrently or downgrading with frozen sessions is unsafe.
Application guards do not prevent manual SQL from bypassing the invariant.

## Customer endpoint

`POST /sessions/{session_id}/checkout` requires a verified Firebase ID token in
`Authorization: Bearer <token>` and an empty JSON object `{}`. The session ID
must be a positive SQLite integer. Ownership comes exclusively from the token.
Every request field is forbidden, including UID, cart ID, amount, total,
currency override, payment status/success, and provider results.

HTTP 200 returns exactly:

```json
{
  "session_id": 42,
  "cart_id": "CART-001",
  "checkout_started_at": "2026-10-03 12:00:00",
  "final_amount": 20.0,
  "currency": "INR"
}
```

Values above are illustrative. Timestamp uses SQLite `datetime('now')` (UTC).
Errors retain the `{ "error": "code", "message": "safe explanation" }` shape.

| HTTP | Error codes / condition |
|---|---|
| 401 | authentication_required: missing/empty token |
| 403 | invalid_token; session_forbidden: foreign/unowned session |
| 400 | invalid_request; invalid_session_id |
| 404 | session_not_found |
| 409 | session_completed; empty_cart; invalid_bill; invalid_session; session_conflict |
| 503 | database_unavailable: SQLite lock or database failure; retry the same session safely |
| 500 | internal_error: checkout could not be confirmed |

Token-verifier errors follow the existing pairing HTTP convention. Errors do not
expose database/SDK exception text. No response authorizes payment or completion.

## Transaction and amount

`edge.billing.checkout.begin_checkout(session_id, verified_customer_uid)` is a
trusted backend operation. The caller must verify the UID before calling it.
One `BEGIN IMMEDIATE` transaction checks the session, owner, cart, unfinished
state and unique active-session invariants for both cart and customer. It reads
every bill row with a LEFT JOIN so missing product data cannot disappear.

Empty carts are rejected without freezing, completing, or releasing ownership.
Duplicate/missing products, invalid quantities/names, negative/non-finite money,
inconsistent line totals or session totals are rejected without repair. Amounts
come from stored bill prices/totals, never catalog repricing or client input.
Bill sum and maintained session total must agree under the existing reconciliation
tolerance (`rel_tol=1e-9`, `abs_tol=1e-9`). The timestamp is persisted and the
existing validated total_amount is preserved as the fixed amount. Current REAL
money storage remains; exact INR-to-paise conversion/rounding is future work.

ADD/REMOVE acquire the same SQLite writer transaction and check both end_time
and checkout_started_at before modifying bill, log or total. A mutation committed
first is included. Once freeze commits, later mutations reject, even if physical
detection or recognition began earlier. A read before recognition is not permission
to commit. Checkout does not prove physical cart contents match SQLite; stop
changing cart contents and resolve physical discrepancies separately.

## Retry, runtime and ownership

Session ID is the checkout identity. Repeating checkout as the same verified
owner returns the same timestamp, amount and cart, including after a lost response
or backend restart. Retries validate source integrity but do not rewrite/reprice
the checkout. Concurrent requests serialize. An old completed-session request
is rejected and never redirected to a newer session.

Runtime startup/resume rejects frozen sessions. An already-running runtime may
observe/recognize an event, but authoritative billing rejects it with a frozen
reason and the existing worker fail-stops, discarding subsequent queued work.
Rejected REMOVE restores in-memory contents. No ADD/REMOVE is replayed.

Frozen sessions remain unfinished and owned: owner reconnect returns the same
session, another customer cannot take the cart, and the owner cannot acquire
another cart. Trusted setters cannot reassign a frozen session. App close/logout
and backend/runtime restart do not unfreeze or release it. Future cancelled or
failed payment leaves it frozen for payment retry against the same bill. There
is no unfreeze operation in phase 1; operational recovery must not bypass this
by clearing timestamps or pretending payment succeeded.

Trusted `CartSession.complete()` remains structurally available and retains the
checkout timestamp. Checkout never calls it. Future backend payment verification
must authorize completion; Flutter cannot declare payment success. Only completion
allows a new session for the next customer while retaining old session history.

## Firebase boundary

Checkout performs no Firebase writes, event pushes, or reconciliation. Existing
projection fields, client-write denial and controlled reconciliation are unchanged.
Checkout state is backend-only. Flutter/Firebase cannot infer shopping permission
solely from end_time. A future checkout UI must recover checkout state through the
authenticated backend API (repeat checkout for the same session), never RTDB writes.
Reconciliation still requires quiescence and preserves event_log; it neither
unfreezes checkout nor reconstructs missing event history.
