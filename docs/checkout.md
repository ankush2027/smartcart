# Checkout generations and controlled cancellation

SQLite remains authoritative. `session_id` identifies the shopping journey;
`checkout_id` identifies one immutable freeze. A session can shop, freeze checkout
#1, cancel #1, undergo operator physical inspection, start a fresh runtime, shop
again, and freeze checkout #2. The IDs differ even when their amounts are equal.
No payment provider, verification, customer completion endpoint or automatic
runtime turnover is implemented.

## Storage and deployment

`checkouts` retains checkout_id (AUTOINCREMENT), session_id, request_key, amount
(REAL), currency (INR), created_at, state (FROZEN/CANCELLED/COMPLETED), cancelled_at
and payment_started_at. Its session foreign key restricts deletion; the backend
connection factory enables foreign keys. Request keys are unique per session;
a partial unique index permits only one FROZEN checkout per session. SQL checks
enforce state/currency/cancellation consistency. Triggers prohibit snapshot
changes, deletion, terminal-row changes and clearing/changing a payment reservation.
Application validation additionally rejects non-finite/negative monetary values.

`sessions.checkout_started_at` remains the compatibility freeze marker. Creating
a FROZEN row and setting it happen in one writer transaction. Cancellation marks
the exact row CANCELLED, sets cancelled_at once, and clears the marker in one
transaction. Disagreement between the marker and history fails closed, without
automatic repair. History is retained permanently; cancelled amounts never follow
later changes to sessions.total_amount.

Stop all old writers before migration. Initialization creates/backfills/validates
checkout history in an explicit BEGIN IMMEDIATE transaction. A legacy unfinished
session with a marker becomes FROZEN; a completed marked session becomes COMPLETED.
Existing timestamp and amount are preserved after validation. Deterministic
`legacy:<session_id>` request keys prevent invented client identities. Unmarked
sessions receive no history. Repeated initialization validates existing history;
it does not conceal mismatches by backfilling missing rows. A failure rolls back
the checkout migration and prevents normal startup.

All billing writers must be upgraded together and use the same database. Old
binaries do not maintain history/generations. Mixed deployment or downgrade with
checkout history is unsafe. Direct administrative SQL remains outside application
authorization; do not bypass lifecycle protections.

## Authentication and initiation

All endpoints below require `Authorization: Bearer <Firebase ID token>` and
verify that the token UID owns the exact session. IDs are positive SQLite integers.
Manager claims do not bypass ownership on these customer endpoints. No UID, cart,
amount, currency, payment status or provider payload is accepted from the body.

`POST /sessions/{session_id}/checkout`

- Body must be exactly `{}`.
- `Idempotency-Key` is required: 1–128 visible ASCII characters, no whitespace.
  The `legacy:` prefix is reserved. Keys are opaque; do not derive them from time.
- A new key creates one FROZEN checkout after complete authoritative bill validation.
- Same key while frozen returns the same ID, amount and creation timestamp.
- Same key after cancellation returns that historical CANCELLED checkout, even if
  a newer checkout exists. It never refreezes the session.
- A different key while a checkout is frozen returns 409.
- After cancellation, a new freeze requires a new key.
- Completed-session POSTs are rejected, never redirected to another session.

HTTP 200 result (illustrative):

```json
{
  "session_id": 42,
  "cart_id": "CART-001",
  "checkout_started_at": "2026-10-05 12:00:00",
  "final_amount": 30.0,
  "checkout_id": 7,
  "state": "FROZEN",
  "cancelled_at": null,
  "runtime_restart_required": false,
  "currency": "INR"
}
```

`checkout_started_at` in this result is that checkout's immutable created_at;
it is retained even when the session compatibility marker is later cleared.
Times use SQLite datetime('now'), UTC. Request keys are never returned.

## Exact-ID cancellation

`POST /sessions/{session_id}/checkouts/{checkout_id}/cancel` with body `{}`.
No Idempotency-Key is required: the exact checkout ID identifies cancellation.

One BEGIN IMMEDIATE validates ownership, session/checkout relationship, unfinished
session, marker/history consistency and payment reservation. Only the current
FROZEN checkout can transition. Already-CANCELLED targets return their historical
result without changing their timestamp or any newer checkout. The response has
the same fields as initiation, with state CANCELLED, persisted cancelled_at and
runtime_restart_required=true. It does not claim that the target is current.

Cancellation retains the bill, total, cart, customer, session and history. It
does not complete or release anything. Duplicate/concurrent cancellation, lost
responses and backend restart return the same persisted result. A non-null
payment_started_at blocks cancellation and is never cleared by retry/error.
No current production operation sets that future payment marker.

## Authoritative state read

`GET /sessions/{session_id}/checkout` reads one consistent SQLite snapshot. Use
this after reconnect; do not POST merely to discover state. No body/key is needed.

The response contains exactly session_id, cart_id, state, current_checkout_id,
latest_checkout and runtime_restart_required. Session state is SHOPPING,
CHECKOUT_FROZEN or COMPLETED. current_checkout_id is non-null only for the current
FROZEN checkout. latest_checkout is null or the checkout result described above,
including historical CANCELLED/COMPLETED state and its immutable amount/currency.

After cancellation the top-level state is SHOPPING, current_checkout_id is null,
and runtime_restart_required is true. This is a conservative operational warning:
the backend does not record inspection clearance or runtime restart acknowledgment,
so it remains true while the latest checkout is cancelled. It is not proof of
physical safety, nor a command to restart automatically. For completed sessions
the top-level warning is false; any nested cancelled result is historical only.

## Errors

Errors use `{ "error": "code", "message": "safe explanation" }` without SQL,
stack traces, credential paths or SDK details. Existing token-verifier conventions
are retained.

| HTTP | Conditions/codes |
|---|---|
| 400 | invalid_request, invalid_session_id (either ID), invalid_idempotency_key |
| 401 | authentication_required |
| 403 | invalid_token, session_forbidden |
| 404 | session_not_found, checkout_not_found |
| 409 | session_completed, empty_cart, invalid_bill, invalid_session, session_conflict, checkout_integrity, checkout_already_frozen, checkout_session_mismatch, payment_started |
| 503 | database_unavailable (including lock timeout): retry the same identity/key |
| 500 | internal_error: outcome not confirmed; recover by same-key/exact-ID retry or GET |

## Amount and transaction ordering

Creation validates every stored bill row with a LEFT JOIN, detecting missing
products, duplicates, invalid quantities/names, negative/non-finite values and
inconsistent totals. Empty carts are rejected without freezing or releasing them.
Stored bill prices/totals are used, never current catalog repricing. The maintained
session total must match the bill sum with existing rel_tol=1e-9/abs_tol=1e-9
semantics and is copied unchanged into checkouts.amount. Frozen retries validate
the source; cancelled snapshots remain independent of today's bill.

No rounding or broad money migration occurs. Before payment integration there
must be one explicit INR-to-integer-paise conversion policy; provider amounts
must eventually be persisted/compared as integers. No conversion exists here.

Cancellation, creation and billing writers serialize through BEGIN IMMEDIATE.
A mutation obtaining the lock while frozen rejects; a valid fresh-generation
mutation after cancellation may commit. A mutation committed before re-freeze is
included; re-freeze committed first blocks it. Invocation/detection time is not
lock order: a waiting old mutation needs generation rejection as well.

## Runtime generation and physical resume

Hydration reads shopping state, cart contents and MAX(checkout_id), or 0 before
the first checkout, in one transaction. CartSession retains that generation;
every production ADD/REMOVE uses a mandatory-generation billing entry point.
The same billing writer transaction checks shopping state and latest generation.
An old runtime never refreshes its generation automatically. Creating checkout
#1 invalidates generation 0 permanently, even after cancellation. Creating #2
invalidates runtimes hydrated at #1. Legacy direct billing functions remain
trusted low-level APIs; they are not the production runtime entry points.

Frozen/stale runtime rejection retains existing fail-stop, queue discard, event
timestamp checks and REMOVE memory restoration. Frozen startup remains rejected.
Cancellation does not resume an old runtime or replay physical events.

Required prototype procedure:

1. Stop/fail-stop the old runtime. If shutdown is uncertain, exit its process and
   honor the quarantined cart-lock procedure before replacement.
2. Cancel the exact checkout.
3. Operator/manager inspects physical contents against the authoritative bill.
4. Resolve discrepancies using the approved physical procedure; never replay a
   rejected ADD/REMOVE to guess the correct bill.
5. Start a fresh runtime and hydrate the current generation, then resume shopping.

Inspection is an operational prerequisite, not an automatically enforced clearance
record. A physical item added while frozen may be unbilled. No recorded rejection
does not prove basket safety. This milestone is unsuitable for unattended automatic
resume; no automatic physical reconciliation or manager API is introduced.

## Ownership, completion and downstream data

Owner reconnect retains the same session after cancellation. Other customers
cannot take the cart, and the owner cannot acquire another cart. Trusted setters
cannot change ownership/cart once checkout history exists. App close/logout does
not cancel or release a session.

Trusted CartSession.complete() remains available. Completing a current frozen
checkout marks it COMPLETED in the same transaction as session end_time, retaining
its timestamp and amount. This does not prove payment. Completing after cancellation
does not relabel cancelled history. Future payment finalization must bind exact
checkout/payment identity, amount, currency and lifecycle in one transaction, e.g.
complete_paid_checkout(checkout_id, verified_payment_record). No customer completion
endpoint or provider verification exists here.

Future payment initiation must reserve this checkout durably before contacting a
provider. If reservation wins cancellation must fail; if cancellation wins payment
creation must fail. Timeout/failure must not silently clear that reservation.

Checkout, cancellation and state reads do not call Firebase. RTDB fields/rules and
controlled reconciliation are unchanged. No checkout/cancellation history is
projected, no ownership is cleared, and no events are pushed/rebuilt/replayed.
Flutter cannot infer shopping permission solely from RTDB end_time. Controlled
reconciliation still requires writer quiescence and does not establish basket safety.
