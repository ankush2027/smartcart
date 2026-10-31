# Cloud edge protocol — design, not an enabled endpoint

The cloud API currently returns 404 for `/edge/events`. Do not point the existing
supervisor at a cloud SQLite file, share SQLite over a network filesystem, or run
the old supervisor beside the cloud API. No Arduino firmware was changed.

## Trust and provisioning

A trusted provisioning command must create a device record bound to exactly one
cart, with a random 256-bit bearer credential shown once, stored in the edge OS
secret store. Cloud stores only its hash and key ID, supports rotation/revocation,
and permits one active writer per cart. Use separate staging credentials. No
Firebase Admin key, customer password or Firebase refresh token belongs on edge
or in the APK. TLS certificate validation is mandatory; redirects are rejected.
Rate-limit by device at ingress. Device compromise permits false sensor claims;
cloud cannot independently prove physical recognition or weight evidence.

## Proposed endpoints

- `GET /edge/context`: authenticated device obtains its cart's active session ID,
  checkout generation, next sequence and shopping/frozen/completed state. Do not
  return customer UID. No active customer means capture must not bill.
- `POST /edge/events`: JSON exact keys `event_id` (UUID), `session_id`,
  `generation`, `sequence` (positive integer), `operation` (`ADD` or `REMOVE`),
  `product_id` (registered active SKU). Reject extra fields, floats/bools as IDs,
  oversized bodies, price, total, customer ID or alternate cart ID. Server derives
  cart from the credential and price from its own catalogue.

Generation is the existing latest checkout ID (zero before checkout). Canceling
checkout still advances the generation: a pre-freeze observation can never be
replayed into resumed shopping. A newly paired customer always gets a new session.

## Required atomic cloud operation

Add `edge_devices`, `edge_event_receipts` and `edge_sequences` tables with unique
constraints on `(device_id,event_id)` and `(device_id,session_id,generation,sequence)`.
These are a proposed migration, NOT installed by this change.

1. Authenticate active device credential before returning even an old receipt.
2. Begin an immediate SQLite transaction; recheck active device/cart assignment.
3. Look up event UUID and canonical request digest. An identical retry returns the
   saved response without touching the bill. Same UUID with different fields is 409.
4. Verify exactly one active session for the device cart, exact session ID,
   customer ownership, current generation and unfrozen shopping state, using the
   existing checkout guards. Do not accept a UID supplied by edge.
5. Require exactly the next sequence. An out-of-order/gapped event is 409.
6. Invoke the existing add/remove business logic **within this same transaction**.
   Its current public helpers open their own transactions: extract transaction-aware
   helpers before implementing this API. Do not implement dedup as a separate write
   before/after `add_item`; either approach can lose or double-bill on crash.
7. Save immutable receipt/digest/response, advance sequence and commit the bill
   and projection outbox together. A crash must commit all or none.
8. Return the stored receipt (historical accepted quantity/total plus identifiers).
   Firebase publication is asynchronous; HTTP retry never replays billing.

Unknown/inactive product, stale session/generation, invalid removal, frozen checkout,
revoked credential and inconsistent ownership fail closed. Invalid events cannot
silently consume sequence numbers or be relabeled for the next customer.

## Durable edge queue and reconnection

The edge adapter persists the exact event and captured context to its own SQLite
spool **before sending**. UUID, generation and sequence never change on retry.
After the motion/YOLO/weight validation pipeline accepts a physical observation,
queue it once and send only the head event for that cart. On a matching receipt,
mark it acknowledged transactionally before sending the next event. Network timeout,
429 and 5xx use bounded exponential backoff with jitter and `Retry-After` support.
No TLS bypass or fake sensor event is permitted.

401/403 suspends delivery for credential repair. 409/422 quarantines the event and
blocks later events for that generation until operator reconciliation. Do not
silently discard failed removals or move buffered events to a new session. On
reconnection, fetch context, compare session/generation and quarantine stale queues.
A lost success response is safe to retry with the same UUID. Checkout remains a
cloud transaction; an offline observation after checkout begins must be rejected.
Expose pending/quarantined counts to the operator so delayed billing is visible.

## Acceptance gate before connecting hardware

Implement and test atomic rollback at every transaction boundary; concurrent same
UUID delivery; altered payload reuse; sequence gaps; revoked tokens; wrong cart;
unknown SKU; frozen/canceled/completed sessions; new-customer replay; disk-full;
server restart; edge restart and lost response. Prove quantity 1 after ten duplicate
requests, quantity 2 after a second intentional UUID, and unchanged receipts across
restarts. These event tests have NOT been run because the protocol is design-only.
Then connect the supervisor through this adapter and calibrate physical sensors.
