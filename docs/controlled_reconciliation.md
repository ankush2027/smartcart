# Controlled Firebase projection recovery

SQLite is authoritative. Firebase is a rebuildable customer-visible projection.
This operator operation repairs one existing active or completed session. It is
not a customer endpoint and is not called automatically by pairing or runtime.

## Before running

Stop relevant runtime, API and other session writers, and resolve in-flight
SQLite/Firebase operations. Do not run recovery concurrently with normal writes.
The CLI does **not** detect or enforce quiescence. There is no new process lock,
distributed lock, version fence, outbox or retry worker in this phase.

After uncertain runtime shutdown, retain the existing process-exit/quarantined
lock procedure. Inspect physical contents and committed SQLite records before
resuming shopping. Reconciliation cannot determine whether an unrecorded physical
gesture occurred. Do not repeat ADD/REMOVE to fix a Firebase discrepancy.

Use the authoritative backend checkout/database and the existing Firebase Admin
configuration (`SMARTCART_FIREBASE_URL` and `GOOGLE_APPLICATION_CREDENTIALS`).
Do not point a second database copy at the same Firebase session namespace.

```powershell
.\.venv\Scripts\python.exe -B -m edge.firebase.run_reconcile 42
```

On Linux/macOS, use the deployed backend Python interpreter:

```sh
python -B -m edge.firebase.run_reconcile 42
```

Replace `42` with the authoritative SQLite session ID. The CLI accepts only this
positional ID plus standard help. It accepts no owner UID, Firebase path, JSON
payload, completion or payment instruction. Run it only through trusted backend
operator access; Admin SDK credentials are not customer authorization.

## What it does

`read_session_projection(session_id)` in `edge.billing.session_projection` opens
the existing database with SQLite URI `mode=ro`, begins one deferred read
transaction, and reads metadata and every bill line from the same snapshot.
It returns immutable `SessionProjection`/`BillItemProjection` records. A LEFT
JOIN ensures a missing product cannot silently disappear from the bill.

Missing sessions, invalid IDs, invalid ownership/timestamps, missing product
names, duplicate products, invalid quantities, non-finite/negative monetary
values and inconsistent totals fail before Firebase is initialized or accessed.
Monetary consistency uses floating-point tolerance rather than changing existing
money storage. Nullable cart/owner fields remain supported. Product names come
from the current catalog, as in `billing.get_bill()`; this cannot recover an
older product name never stored in the bill. Prices and line totals come from
the bill, not current catalog prices. Corrupt source data is refused, not fixed.

The read connection is closed before network I/O. Recovery never creates a
database, writes SQLite, opens a SQLite write transaction, or recalculates and
persists totals. Existing public billing APIs remain unchanged.

`reconcile_session_to_firebase(session_id)` in `edge.firebase.reconcile` then
publishes one session-level `update()` containing:

- `cart_id`, `customer_uid`, `start_time`, `end_time`, `total_amount`;
- the complete `bill_items` map keyed by product ID, with the established
  `product_name`, `quantity`, `price`, and `total` fields.

The `bill_items` child is replaced as a whole. Missing items are restored;
incorrect values and Firebase-only extra items are removed. An empty SQLite
bill publishes `bill_items: null`, removing stale bill items. Null metadata
follows existing RTDB null/deletion semantics.

The update does not replace the whole session node. Existing `event_log` and
unrelated children survive unchanged, and other sessions are untouched. No
event is pushed or reconstructed. Missing/duplicated Firebase audit events
remain deferred because push keys are not mapped to SQLite `log_id`.

For completed sessions, the stored `end_time` and final total are published
without calling `complete()` again. The session is never reopened, completed
again, reassigned, or cleared. This operation interprets no payment state.

## Results and failures

The coordinator returns `ReconciliationResult(session_id, bill_item_count,
completed)` after the write returns successfully. The CLI prints a concise
success message and exits zero. Invalid CLI arguments exit 2; recovery errors
exit 1 without printing SDK exception details, credentials or session contents.

Source failures do not touch Firebase. Firebase failures raise
`ReconciliationError`; SQLite remains unchanged. A remote failure can mean the
write was applied but its acknowledgement was lost. The CLI reports failure or
an uncertain outcome, not proof that nothing happened remotely.

After resolving in-flight work and restoring controlled conditions, another
invocation reads a fresh SQLite snapshot and can converge to the same projection.
It does not reuse a cached payload, replay billing, or append events. There are
no application retries; the installed SDK may perform its own transport retries.
This is repeatable state replacement, not exactly-once delivery.

An old in-flight write or a concurrent normal writer can still overwrite the
repaired projection. This phase provides no ordering/fencing guarantee. A
successful recovery does not establish physical-cart consistency or authorize
automatic runtime restart.

Restart and same-user reconnect alone do not repair Firebase. Automatic
reconnect repair, event-history reconstruction, durable retries, stale-write
fencing, runtime turnover, checkout and payment remain deferred.
