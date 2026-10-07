# Offline payment core

SQLite is the payment/lifecycle authority. The Razorpay adapter is tested with
mocked HTTP only; there is no payment SDK, real-money flow, public webhook, or
Flutter implementation. The
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

The Phase B inbox and explicitly invoked recovery/processing workers operate
offline with injected providers. There is no public webhook route or background
activation. HTTPS ingress and real provider activation remain deferred.

Current LAN HTTP is development-only. Later payment deployment requires protected
HTTPS transport, an explicit Firebase revocation policy and protected webhook ingress.
Never expose LAN port 5000 publicly. Live provider activation, real money,
merchant onboarding, refunds, partial/split payments, multi-provider deployment,
offline payments, automatic abandoned-payment cancellation, physical reconciliation,
Flutter UI changes and automatic runtime/session turnover are not implemented.

## Razorpay Phase A: injected adapter, mocked HTTP only

`edge.payment.razorpay.RazorpayProvider` implements the existing provider contract.
It requires explicit mode (`test` or `live`), merchant account ID, matching-mode
key ID, API secret, separate webhook secret, HTTP transport and a trusted
`order_lookup(scope, order_id) -> OrderRequest | None` callback. The callback must
read the exact persisted payment attempt by scoped provider order ID, return its
immutable identity/money fields, and close the read transaction before returning.
Never derive that identity from client data or provider notes. Tests exercise this
callback against isolated SQLite, including reconstruction after a lost response.
There is no cache, concrete network transport, configuration loader or startup wiring.

The injected transport implements `HttpTransport.request` and returns
`HttpResponse(status_code, body_bytes)`. Every request specifies the fixed HTTPS
Razorpay API origin, Basic authentication, TLS verification, no redirects, zero
retries and explicit connect/read deadlines (3.05/10 seconds by default).
The transport must enforce these options, including no hidden POST retry and a
bounded response read. The adapter also bounds JSON bodies to 1 MiB, rejects
duplicate JSON keys/non-finite values, and suppresses raw response/transport errors.
Do not log transport authorization headers or exception locals. No SDK is required.

Creation uses the persisted integer paise value, INR, `partial_payment=false`, and
a stable 35-character receipt (`sc_` plus the backend attempt UUID). The local
minimum is 100 paise; smaller reservations receive authoritative rejection without
sending HTTP. HTTP 400 is terminal only for the structured business input-validation
rejection on amount/currency at payment initiation. Other errors, receipt conflicts,
timeouts, malformed/mismatched success responses, 429 and 5xx remain unresolved in
the core. A create call dispatches exactly one POST; service retries use recovery.

Recovery performs GET receipt searches with count=100/skip pagination, then fetches
the selected order by ID. The receipt filter is documented as a contains search,
so exact receipt equality is checked locally along with amount, INR, order identity
and any returned scope fields. Zero matches remain unresolved. Multiple matches,
conflicting records or incomplete pagination require inspection. The default limit
is 100 pages; reaching it fails closed. Missing `partial_payment` follows the
documented false default; a returned value other than false is rejected.
No empty lookup or elapsed time authorizes another POST. Receipt lookup is not a
documented atomic idempotency or visibility guarantee; operator recovery remains
necessary for unresolved attempts. Failed customer payments do not create a new
order or attempt under the current one-attempt-per-checkout policy.

Checkout evidence contains only `payment_id` and `signature`. HMAC-SHA256 uses the
server-persisted order ID and API secret, compared in constant time. The adapter
independently fetches both payment and order, binds the order to the persisted
attempt via the callback and exact receipt, and requires integer amount equality,
INR, status=captured, captured=true, zero amount_refunded and null refund_status.
Only then is CAPTURED normalized for the unchanged core verification/completion
transactions. Authorized, failed, refunded, stale, substituted or mismatched
evidence cannot complete a checkout. A valid signature alone cannot pay a bill.

Ordinary Razorpay order/payment responses do not consistently carry account/mode
fields. Their scope comes from the configured credential context; returned scope
fields, when present, must agree. The key prefix checks environment, but cannot
prove that a configured merchant ID owns those credentials. Controlled onboarding
must verify that binding before any real transport is enabled. Test/live credentials
and webhook secrets must be separate. No such validation or API call occurred here.

`verify_webhook(raw_body, signature, event_id=header_value)` authenticates the exact
raw bytes with the webhook secret and checks account and payment-event structure.
It supports payment.captured, payment.authorized and payment.failed and returns the
existing contract's `(event_id, payment_id)` tuple. It performs no HTTP or database
work. The required Razorpay event ID comes from `x-razorpay-event-id`; that header is
not covered by the body HMAC and is only a delivery hint. Phase B binds a body
digest and independently verifies provider facts before payment completion.
No HTTP receiver is exposed.

Official references checked for this adapter:

- [Create order: money, receipt, partial payments and errors](https://razorpay.com/docs/api/orders/create/)
- [Receipt lookup and pagination](https://razorpay.com/docs/api/orders/fetch-all/)
- [Fetch payment fields](https://razorpay.com/docs/api/payments/fetch-with-id/)
- [Checkout signature verification](https://razorpay.com/docs/payments/payment-gateway/flutter-integration/standard/integration-steps/)
- [Raw webhook signature and duplicate-event guidance](https://razorpay.com/docs/webhooks/validate-test/)

Tests use dummy secrets and mocked HTTP with socket connections forbidden. They
cover payloads, minimum/strict amounts, ambiguity, receipt recovery/pagination,
signature tampering, capture/refund checks, scope isolation, redaction and durable
service recovery/completion. Real test-mode validation, production transport,
merchant binding, secure ingress and Flutter remain deferred.

## Phase B: durable offline webhook ingestion and recovery

No provider is selected automatically. Normal server startup and customer HTTP
routes are unchanged, and no webhook route, scheduler, daemon or public listener
has been added. All worker entry points require explicit service/provider injection.
Use fake providers or mocked HTTP only in this milestone; no credentials are needed.

Initialization adds three tables inside the existing atomic checkout/payment
migration: `payment_webhooks`, `payment_webhook_conflicts` and
`payment_recovery_jobs`. Stop older writers before migration. This code does not
rewrite payment, checkout, bill or customer identities.

The inbox stores an internal ID, provider scope/event ID, event type, SHA-256
payload digest, authenticated raw bytes, provider order/payment IDs, received time,
processing state/count, last attempt time, next eligible retry time, claim token/
expiry, sanitized error code and processed time. Timestamps use integer Unix seconds.
Scoped event IDs are unique. Raw evidence/identity cannot be updated, deleted or
replaced. Conflicting authenticated bodies are retained separately under the inbox
record, with their digests and normalized identifiers. They never overwrite the
original evidence. Protect the SQLite file and backups: raw provider bodies may
contain personal information. Diagnostics never return those bodies or signatures.

`WebhookIngestor(provider).ingest(raw_bytes, signature, event_id=delivery_id)` calls
the adapter's `authenticate_webhook` before opening a writer transaction. Razorpay
uses its existing raw-byte HMAC verification and requires the event ID extracted
from `x-razorpay-event-id`. Authentication returns an `AuthenticatedWebhook` with
scope, event/type and order/payment identifiers, not a payment result. The fake
uses a signed offline JSON envelope containing event_id, event_type, order_id and
payment_id. Its older two-field signature helper remains supported for existing tests.
Malformed, oversized, invalidly signed or mismatched-scope deliveries are rejected.

Only after `BEGIN IMMEDIATE`, evidence insertion and successful COMMIT does ingestion
return an acceptance result. Identical duplicates return the existing ID/state.
Same-ID/different-body deliveries retain the new evidence and quarantine the inbox
record, including one previously processed; already committed payment completion
is never reversed. Database/commit failure raises, so a future HTTP boundary must
not acknowledge success. Ingestion performs no payment finalization or provider I/O.
The event-ID header remains unsigned: replay with a different header can produce
another inbox record, but exact persisted payment identity makes completion idempotent.

### Explicit processing and claims

Construct `WebhookProcessor({provider.scope: payment_service})` and call
`run_once(webhook_id)` or `run_batch(limit=20, after_id=0)`. A batch accepts at most
100 records and returns `(webhook_id, outcome)` pairs. Use the returned IDs as
cursors for further bounded batches; no unbounded drain loop runs automatically.

States are RECEIVED -> PROCESSING -> PROCESSED, RETRYABLE or QUARANTINED.
Eligible RETRYABLE records return to PROCESSING. Each atomic claim increments the
attempt count and gets a fresh random token and lease. Defaults are a 120-second
lease, 30-second retry delay and five total attempts (`work.RetryPolicy`). No sleeps
or external scheduling happen in this library. Exhausted budgets quarantine work.
PROCESSED and QUARANTINED are terminal, except digest conflicts may quarantine a
PROCESSED inbox record. No API for clearing quarantine or forcing payment success exists.

PROCESSING records are not reclaimed automatically. After inspecting an interrupted
worker, explicitly pass `reclaim_expired=True`; only an expired lease can be reclaimed.
Worker clocks must share the host's Unix-time basis. Tokens and expiry are rechecked
inside the same SQLite transactions that save verification and complete payment.
Expired/superseded claims and concurrent quarantine cannot authorize those writes.
A provider response arriving after claim loss is discarded. No network call holds
a SQLite writer transaction. Application or database failures retain/retry durable
work; raw exception strings are never persisted or logged by these workers.

The processor resolves only the configured scope and exact persisted order, then
uses `PaymentService.verify_attempt` for an independent provider fetch and the
existing `store.save_verified` checks. Callback verification shares that same path.
Only `complete_paid_checkout` can consume VERIFIED evidence and atomically finish
the exact checkout/session. No amount-based matching, generation rebinding, payment
reservation replacement or automatic cancellation/cart release is introduced.
Unknown orders remain RETRYABLE, then quarantined when the configured budget expires.
Noncaptured or temporarily unverifiable facts retry within that budget. Identity
conflicts quarantine immediately. A webhook's stated status never decides payment.

### Crash behavior

- Before ingestion commit: rollback/no acknowledgement; provider delivery can retry.
- After ingestion commit or lost acknowledgement: evidence survives and duplicate
  ingestion returns the same record.
- During fetch/before verification commit: reclaim and independently fetch again.
- After VERIFIED commit/before completion: retry the exact persisted completion;
  a provider outage does not erase authoritative verification.
- After completion/before PROCESSED: same-payment replay marks the inbox processed
  without fetching again, re-completing, or repeating the projection hook.
- A delayed failure cannot downgrade VERIFIED/COMPLETED. Different payment evidence
  for an already consumed order is quarantined.

If a conflict arrives after verification was already committed, it stops that
inbox worker, but does not erase independently verified SQLite payment evidence.
Controlled payment recovery may still complete that exact verified attempt.

### Controlled payment recovery

`RecoveryWorker({provider.scope: payment_service}).run_once(payment_attempt_id)`
accepts an existing attempt only. `run_batch([attempt_ids])` accepts 1–100 IDs.
Durable per-attempt work records use the same claim, retry and explicit reclaim
policy as the inbox. No public worker endpoint or automatic network timer exists.

| Payment state | Recovery action |
| --- | --- |
| RESERVED | Existing atomic dispatch claim; only its winner may create once. |
| CREATING | Provider recovery/lookup only; an empty lookup never authorizes POST. |
| ORDER_READY | Preserve the order; process up to 20 authenticated inbox hints for that exact scope/order. Without a hint, wait for evidence. |
| VERIFIED | Retry the existing atomic paid completion. |
| COMPLETED | Return without a new fetch, payment or completion. |
| FAILED | Return without resurrection. |

Waiting for evidence can exhaust a recovery job's budget. This requires inspection;
it does not mark the payment FAILED, clear its reservation, or prevent a later valid
customer callback or separately processed webhook. Unresolved/quarantined records
are retained. This milestone intentionally provides no operator override to mark
paid, unfreeze a cart, create another order, or bypass provider verification.

`store.order_request(scope, order_id)` is the trusted persisted-order callback for
the Razorpay adapter and closes its SQLite connection before HTTP. Ordinary provider
scope remains tied to explicitly configured credentials; real merchant binding and
receipt-lookup visibility guarantees remain unverified Phase A limitations.

SQLite completion always commits before the optional `completion_sync` hook. Only
the fresh completion winner calls it. Failure leaves payment/session COMPLETED;
controlled Firebase reconciliation remains available. A crash may omit delivery;
this is not a durable Firebase outbox. No live Firebase calls, payment RTDB records,
rules changes or automatic reconciliation were introduced.

### Read-only operator inspection and deployment boundary

Run `python -B -m edge.payment.diagnostics --database PATH --limit 100` locally.
It opens an existing database in SQLite read-only/query-only mode and never runs
migration or creates a missing file. Payment rows expose attempt ID, checkout ID,
scope, state, recovery status and sanitized reason. Inbox rows show internal IDs,
scope/state, attempt count/time, reason and any exact matching attempt/checkout.
Use `--after-attempt ID` and `--after-webhook ID` for bounded pagination. Reports
exclude customer identity, raw bodies, signatures, tokens, API/webhook secrets and
claim tokens. Errors are generic. There is no mutating diagnostic command.

Future HTTPS ingress requires a separately approved deployment, authenticated raw
body handling, strict request-size limits, signature headers, acknowledgement only
after durable commit, protected SQLite storage, merchant/test-mode validation and
operational retry/quarantine monitoring. Genuine provider webhooks use signatures,
not customer bearer tokens. Do not expose the current LAN server or enable real
Razorpay transport as part of Phase B. Phase C and Flutter integration remain deferred.
