# Offline payment core

SQLite is the payment/lifecycle authority. This milestone has no real provider,
payment SDK, real-money flow, public webhook, or Flutter implementation. The
normal server calls `create_app()` without a payment service: payment endpoints
return controlled `payment_unavailable` (503), never fall back to a fake.

## Identity and persistence

The chain is provider payment -> payment attempt -> exact checkout ID -> session.
Neither equal amounts nor a matching cart/customer/session can substitute for
checkout identity. Cancelled checkout #1 cannot pay current checkout #2, even
when both amounts are equal. One durable attempt per checkout is enforced by
UNIQUE(checkout_id); no second order is permitted by changing the request key.

Initialization migrates checkouts and payment schema in the same BEGIN IMMEDIATE
transaction. It adds nullable checkouts.amount_paise without populating historical
rows. Reservation initializes it exactly once together with payment_started_at
and the RESERVED attempt. No existing REAL value is rewritten.

payment_attempts stores backend UUID identity, checkout FK (delete restricted),
payment request key, provider scope, amount_paise, INR, state, nullable order/payment
IDs, created/updated/verified/completed timestamps and a bounded machine error code.
Provider scope includes provider, environment and account. Scoped non-null order
and payment IDs are unique. Identity, money and established evidence are immutable;
history cannot be deleted and transitions/terminal states have SQL guards.

The attempt is authoritative; payment_started_at is its compatible sticky marker.
Paise, timestamp and attempt inconsistencies fail closed and require inspection.
Existing standalone markers remain cancellation-blocking, never permission to
create another reservation. Initialization reports inconsistent existing records.
Stop old writers before schema migration; mixed-version operation is unsupported.

## Exact money boundary

`money.to_paise()` accepts exact Python int/float inputs from trusted SQLite REAL
checkout data, rejects bool/unsupported types, negative/non-finite values, uses
Decimal(str(value)) and multiplies by 100 with explicit sufficient precision.
The result must be exactly integral; no rounding or tolerance is applied.

10 -> 1000; 10.5/10.50 -> 1050; 10.005 is rejected. Zero converts to zero but
reservation rejects a zero payment. Tiny artifacts such as 20 + 1e-10 are rejected
without changing the valid historical checkout or bill. This intentionally differs
from legacy checkout float tolerance. Decimal objects are not database input types.

The project development cap is **10,000,000 paise (INR 100,000)**. Inject `max_paise`
into PaymentService (or begin_payment) to configure it; configuration must be a
positive integer <= 2^53-1, within both JSON safe-integer and SQLite INTEGER range.
No provider maximum is assumed. A future adapter must enforce its documented limit.
Conversion happens only at first reservation. All later payment comparisons use
the two immutable integer snapshots; existing attempts retain their original cap
decision across retries. Broad historical monetary migration is deferred.

## Transactions, state and recovery

RESERVED -> CREATING -> ORDER_READY -> VERIFIED -> COMPLETED.
CREATING -> FAILED is allowed only for authoritative rejection proving no payable
or successful order exists. Individual failed customer payments leave ORDER_READY;
they do not prove the provider order is terminal. No timeout becomes FAILED.

Reservation validates authenticated owner, active ownership, history, exact current
FROZEN checkout, unfinished session, bill and money under BEGIN IMMEDIATE. It resolves
existing payment idempotency and atomically writes the snapshot, marker and attempt.
The payment Idempotency-Key has an independent namespace from checkout keys. Same
checkout/key returns or recovers that attempt; another key conflicts. Exact completed
attempt retries also return persisted completion, covering a lost successful reply.

A separate short transaction claims RESERVED -> CREATING. Only the claimant calls
create_order. Provider work never holds a SQLite transaction. Success is persisted
as ORDER_READY in another transaction. A timeout, unexpected adapter exception or
lost response leaves CREATING with recovery_required. A process may have crashed
after claiming but before sending: this is also ambiguous, not evidence of absence.

Same-key retries of CREATING call recover_order for that same attempt. The adapter
must guarantee recovery safety even while the original create is still running.
No timeout lease or ordinary not-found response authorizes blind redispatch. An
unresolved lookup leaves CREATING. Safe remote idempotent recovery may return the
original order or atomically ensure that same order; this is an explicit adapter
contract. Different scope or conflicting returned identity fails closed.

FakeProvider is explicitly injected, deterministic and offline. FakeLedger models
the remote service and is retained when reconstructing the backend service/adapter
in restart tests. It supports rejection, timeout, lost response, order lookup,
payment lookup, captured/failed payment records and signed evidence. Optional
recover_missing uses an atomic idempotent ledger operation, not an absence heuristic.
The ledger is in memory; restarting the entire fake host loses it and requires
operator attention for unresolved persisted attempts. It is not a real provider.
Its public test signing key provides no production security. Never deploy it for
real payments or treat externally minted fake evidence as real-money proof.

## Verification and completion

Client evidence is an input to adapter authentication, then independent payment
retrieval. The core checks scope, exact attempt, exact checkout, order/payment IDs,
integer amount, INR and CAPTURED status. A client success/verified flag is rejected.
Verified evidence is persisted before attempting local completion; payment identity
uniqueness prevents reuse. Duplicate evidence is idempotent; a different payment
conflicts and late failure cannot downgrade VERIFIED/COMPLETED.

complete_paid_checkout(checkout_id, attempt_id) loads trusted persisted verification.
One writer transaction revalidates exact relationships, current frozen checkout,
unfinished session, ownership, reservation, amount/currency and bill integrity,
then completes checkout, consumes payment and sets session end_time atomically.
The exact frozen REAL total is preserved. Rollback leaves VERIFIED for a safe retry.
Exact duplicate completion returns persisted success; mismatched identity conflicts.

Generic billing.complete_session()/CartSession.complete() remain trusted NON-PAYMENT
operations for legacy/admin/test flows, but reject any session with a payment marker
OR payment attempt inside their writer transaction. They cannot bypass reservation.
Unreserved trusted completion remains an explicit exception to a universal paid-only
policy; no customer generic-completion endpoint is introduced.

## API and projection

Explicit construction: `PaymentService(FakeProvider())`, injected as
`create_app(payment_service=service, token_verifier=offline_verifier)` for tests.
No run_server configuration silently selects a fake. Normal authentication remains
unchanged; do not disable real authentication on a deployed server.

- POST /sessions/{sid}/checkouts/{cid}/payment: Bearer token, payment Idempotency-Key,
  empty JSON object. Reserves/dispatches or recovers the same attempt.
- GET the same path: owner-only authoritative local status; no provider call.
- POST the same path plus /evidence: only payment_id and signature strings.
  Authenticates/fetches evidence, verifies and completes the exact checkout.

Responses use explicit public fields, never provider account scope, request keys,
raw provider payloads, credentials or raw exceptions. Pending/ambiguous status is
returned with state CREATING and recovery_required; it is not a failed payment.
Verification unavailability/database unavailability uses a controlled retryable 503.

PaymentService accepts an optional explicit completion_sync(session_id) hook. Only
the first successful local completion invokes it, after commit. There is no live
Firebase default. Failure returns COMPLETED with reconciliation_required; duplicate
completion does not resend or re-run payment. A crash after commit may omit sync.
projection_sync describes only this call, not a durable delivery guarantee.

Existing controlled reconciliation can repair session metadata/bill, preserves
event_log and never republishes payment attempts. Quiesce relevant writers first;
automatic delivery, outbox, stale-write fencing and automatic reconciliation are
not implemented. RTDB schema/rules remain unchanged.

## Cancellation, runtime and cart release

Cancellation and reservation serialize using BEGIN IMMEDIATE. Cancellation first
prevents reservation; reservation first prevents cancellation. Failure and ambiguity
never clear the reservation, unfreeze shopping or enable another attempt. There is
no customer cancellation/replacement attempt even after FAILED in this milestone;
operator recovery policy remains future work.

Payment never changes runtime generation or clears checkout_started_at, never resumes
an old runtime and never authorizes ADD/REMOVE. Existing fail-stop/quarantined-lock
and physical inspection procedures remain. Only paid completion commit releases a
payment-reserved session through end_time. Pairing then creates a fresh session for
the next customer. No unlock flag, runtime restart or physical-cart clearance is added.

## Deployment and deferred work

No webhook processor, inbox or public webhook route is implemented. The adapter
contract includes raw-byte webhook authentication, tested offline on the fake, but
event ingestion/durable deduplication remains a later integration milestone.

Current LAN HTTP is development-only. Later payment deployment requires protected
HTTPS transport, an explicit Firebase revocation policy and protected webhook ingress.
Never expose LAN port 5000 publicly. Razorpay/Juspay/Stripe integration, real money,
merchant onboarding, refunds, partial/split payments, multi-provider deployment,
offline payments, automatic abandoned-payment cancellation, physical reconciliation,
Flutter UI changes and automatic runtime/session turnover are not implemented.
