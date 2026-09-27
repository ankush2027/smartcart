# Stage 7: read-only RTDB repository foundation

Historical Stage 7 report: the composition statements below describe Stage 7. Stage 8 now connects the customer UI to Firebase and adds pairing; manager remains mock. See [Stage 8](STAGE8_PAIRING.md).

Production composition in `lib/app.dart` still uses MockCartRepository and MockManagerRepository. Firebase Authentication remains live. No RTDB repository is instantiated by production UI, and no customer/manager controller was switched to streams. SQLite remains authoritative; Flutter has no cart, billing, session, assignment, product or event write API.

## Final deployed contract — previous gaps RESOLVED

Backend commit `ca522a9 feat: add secure customer cart pairing` is authoritative.
The user confirmed that this contract was committed, pushed and deployed to
Firebase project smartcart-16807. No rules were deployed or changed by this
Flutter alignment, and no live-project test was performed here.

Customer discovery uses exactly:

```dart
FirebaseDatabase.instance.ref('sessions')
    .orderByChild('customer_uid').equalTo(authenticatedUid)
```

The UID comes from the Stage 6 authenticated user, never a caller-supplied
session/cart ID. The server constrains ownership before data reaches Flutter;
local ownership validation is defense in depth. There is no unrestricted
customer `/sessions` read, limit/pagination that could conceal active-session
conflicts, or duplicate UID lookup structure.

After mapping and checking every returned session's ownership:

- Zero active owned sessions emits null (normal no-active-cart state before pairing).
- Exactly one selects its actual Firebase path key and live billing data.
- Multiple active sessions produce RepositoryFailureCode.multipleActiveSessions.
- Missing/null end_time means active; valid present end_time means completed.
  Invalid data produces a controlled failure. Completed history is neither
  selected nor deleted. An unexpected foreign session is never displayed.

The deployed manager grant uses only trusted `role == "manager"` and permits
the manager collection read. No legacy boolean manager claim is used. Product
reads remain manager-only; these repositories do not need product reads.
Rules remain the actual authorization boundary and deny all client writes.

## Repository and listener contracts

FirebaseSessionReader exposes plain snapshot values behind a read-only
transport interface. Its customer operation applies both query constraints
before subscribing to onValue; only its manager operation reads the collection
without an ownership query. No Firebase types enter widgets.

FirebaseCartRepository implements the existing snapshot interface plus the
optional real-time interface. It shares a query among concurrent consumers,
replays current state only within that identity, and releases both listeners
when the final consumer cancels. The first snapshot method cancels its own
subscription after returning. No session ID is required for discovery.

UID changes clear cached cart state and emit null, cancel the old database
listener, then start the new UID's constrained query. Logout clears state and
cancels database reads; the auth observer can resume discovery on a later login.
Generation and identity checks suppress queued old-user values and errors.
Disposal closes all listeners. Invalid snapshots clear the cache and emit a
safe error; consumers must clear their displayed data on errors. The query
can deliver corrected snapshots later without manual repository recreation.

The manager repository retains its identity-bound scoped listener and must be
recreated after identity changes. It checks the trusted app-level manager role;
server permission remains mandatory. SDK permission failures stay controlled.

Stage 8 will add Cart ID UI and call authenticated POST /pair-cart. Neither
pairing nor an HTTP dependency/base URL is introduced here. Production
composition still uses MockCartRepository and MockManagerRepository; no
customer/manager controller has been connected to Firebase streams.

## Mapping and model choices

Session path keys become displayId/sessionId. `cart_id` and `customer_uid` remain nullable when unassigned, as supported by backend main. Start/end times are preserved as DateTime. Backend SQLite `datetime('now')` strings are interpreted as UTC; explicit ISO offsets are honored. Null/missing end_time means active; a valid present end_time means completed. Invalid required timestamps and reversed end/start times are controlled data errors.

Bill child keys become product IDs; product_name, quantity, price and total map directly. Item count sums validated quantities. The existing subtotal model requires a value, so its presentation subtotal sums supplied line totals; the final bill always uses supplied total_amount, even when different. No Firebase subtotal or itemCount is expected or written.

All domain/UI money remains integer paise. Finite nonnegative int/double rupee inputs are converted using decimal digit arithmetic on their received decimal representation, with half-up rounding to two decimals (1.005 → 101 paise, 2.675 → 268). This avoids binary multiplication tie errors; it cannot recover precision already lost by the backend number. Negative, nonnumeric, nonfinite and values exceeding the cross-platform exact integer range are rejected.

Event IDs retain push keys; timestamp, event_type and product_id are preserved. ADD/REMOVE get neutral product-added/product-removed labels; unknown event types get “Cart activity.” No theft/security classification or severity is inferred. Event IDs are scoped to their session (use sessionId + id for any future global UI key).

ManagerOverview.activeCarts includes only derived-active sessions; its activity can include completed-session events, sorted newest first. The alerts list is always empty because this schema has no alert records. ManagedCart.updatedAt is now nullable and left absent by the Firebase mapper. Real start/end times remain separate fields; event time is not relabeled as cart freshness. The sole widget adaptation skips an absent Updated line; existing mock timestamps and UI output remain unchanged.

No products repository was added: bill items already carry the names and prices needed here. Product barcode, yolo_class and expected_weight are not read or duplicated.

## Parsing and testing boundary

Null/empty collections and RTDB numeric-key collections decoded as lists are supported. Malformed optional event records are skipped. Invalid required session/billing values fail with a controlled RepositoryFailure rather than silently publishing an incomplete bill. SDK permission errors and other internal failures become safe app-level codes/messages without raw SDK text. No passwords, tokens or snapshot contents are logged.

The offline tests use fake auth, transport and Firebase SDK objects. They cover mapping, rounding, malformed data, ownership, missing discovery, role gates, cancellation, shared listeners, stale/queued events, and permission failures. Existing Stage 1–6 tests remain intact. No live Firebase users, data, claims or rules are created or modified.

Manual follow-up during later UI integration: verify live customer queries,
manager reads, foreign ownership denial, and Android reconnect behavior using
authorized accounts. Automated validation remains offline. No live security
or emulator validation is claimed by this Flutter task.

## Validation and review inventory

Final alignment validation: `dart format .` passed (39 files, zero final
formatting changes); `flutter analyze` reported no issues; `flutter test`
passed all 105 tests; `flutter build apk --debug` succeeded. The build emitted
non-fatal Java native-access and FlutterFire Kotlin migration warnings.
`git diff --check` passed. The original Stage 7 mapping/money tests and all
Stage 1–6 tests remain unchanged. Repository tests now cover discovery and
identity switching.

Alignment modified only README.md, this report, firebase_session_reader.dart,
repository_failure.dart, firebase_cart_repository.dart,
firebase_manager_repository.dart, and rtdb_repository_test.dart. All earlier
Stage 7 files were preserved. No dependency was added during alignment.

Created files:

- `STAGE7_RTDB.md`
- `lib/core/data/firebase_session_reader.dart`
- `lib/core/data/repository_failure.dart`
- `lib/core/data/scoped_realtime_read.dart`
- `lib/core/data/session_mapper.dart`
- `lib/features/customer/data/firebase_cart_repository.dart`
- `lib/features/manager/data/firebase_manager_repository.dart`
- `test/rtdb_mapping_test.dart`
- `test/rtdb_repository_test.dart`

Modified files:

- `README.md`
- `lib/features/customer/data/cart_repository.dart`
- `lib/features/customer/models/customer_cart.dart`
- `lib/features/manager/data/manager_repository.dart`
- `lib/features/manager/models/manager_overview.dart`
- `lib/features/manager/screens/manager_screen.dart`
- `pubspec.yaml`
- `pubspec.lock`

Only firebase_database 12.6.0 was added directly, together with its two FlutterFire transitive dependencies. All changes are unstaged in the app directory. The pre-existing untracked root .idea/ remains untouched. No commit, push, branch switch, merge, rebase or cherry-pick was performed. No service-account file was opened or modified, and no credentials were exposed.
