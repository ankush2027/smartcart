# Stage 9 — Manager Live Monitoring

## Architecture and authorization

Production now composes FirebaseManagerRepository with the existing FirebaseAuthRepository. ManagerScreen → ManagerController → RealtimeManagerRepository → FirebaseSessionReader → RTDB. MockManagerRepository remains available only through explicit injection for tests/demos; failures never select it automatically. No new dependencies.

Stage 6 resolves manager access solely from the trusted Firebase ID-token custom claim `role == "manager"`. No email, UID, local selector, or debug configuration grants production manager access. Before a collection subscription starts, the repository requires an authenticated manager from that auth boundary. The deployed Firebase rules remain the actual authorization boundary. No Admin SDK, claim assignment, client writes or rule changes are introduced.

## Data and presentation

Only `/sessions` is consumed, including each session's `bill_items` and `event_log`. `/products` is unnecessary: bill items already supply the displayed product names and prices.

- Firebase session path key supplies session ID; `cart_id`, `customer_uid`, `start_time`, `end_time`, and `total_amount` supply metadata.
- Null/missing `end_time` means active; a valid non-null value means completed. Both active and completed sessions appear in the dashboard. Status labels are derived presentation values, never backend fields or writes.
- Bills use `product_name`, `quantity`, `price`, and `total`, preserving product child keys. Item count sums supplied quantities. Empty bills display “No bill items.”
- Event IDs preserve Firebase push keys, scoped by session ID. Timestamp, event_type and product_id are preserved. ADD and REMOVE are neutral “Product added”/“Product removed” labels; unknown types display “Cart activity.” Events from active and completed sessions appear newest first. No theft decisions or severity classification are made.
- The production alert section is omitted because the authoritative contract supplies no alerts. Mock alert presentation remains available for existing demo tests. No invented alert, severity, updatedAt, stored status or subtotal field is read. RTDB supplies no freshness timestamp, so production displays actual start/end times only.

Existing Stage 7 rupee-to-paise conversion is reused unchanged: decimal half-up rounding at the model boundary; all application money remains integer paise. Displayed total uses `total_amount`, even when it differs from the sum of line totals. Widgets do not recalculate authoritative bills.

## Listener lifecycle

The app-owned repository can be constructed before login. Each subscription binds a new ScopedRealtimeRead to the authenticated identity at subscription time. The manager controller holds one subscription; retry cancels it before creating another. There is no polling. Each `/sessions` onValue snapshot replaces the overview, so new sessions, bills, totals, ADD/REMOVE events and completion update automatically.

Scoped reads observe authentication, invalidate permanently on UID/role loss, cancel database and auth listeners, clear cached data and reject old queued events. A later manager login or explicit retry creates a fresh scoped read. Pause/resume is forwarded to retain the scoped delivery-time authorization check. UID-keyed screens dispose old controllers on identity change; controllers clear data, cancel timers/listeners and ignore old callbacks using generations. The app disposes its owned manager repository. Injected repositories remain the caller's responsibility.

## Loading, errors and empty states

Initial discovery shows loading, bounded at 20 seconds. A missing snapshot then shows safe retry feedback; a later valid snapshot can still recover the view. Read/permission/mapping failures clear prior data and display safe messages. Retry opens a new authorized read rather than reusing a terminal failed stream. Raw Firebase errors and snapshot values are not logged or shown as exceptions.

No sessions yields zero counts, “No active carts,” “No completed sessions,” and “No recent activity.” Empty bills and event logs are valid. Nullable/missing cart/customer metadata displays “Not assigned.” Malformed optional events are skipped; malformed required session/billing values (or invalid non-null metadata) produce controlled data errors rather than fabricated defaults. One invalid session currently fails the full overview.

## Offline tests and validation

`test/manager_live_test.dart` covers active/completed mapping, immutable bill data, authoritative totals, push-key event IDs, optional/malformed data, live session/bill/total/event/completion updates, pre-login repository construction, retry after permission denial, identity loss, cancellation, production composition without mock fallback, and live UI/logout. Existing auth tests retain manager custom-claim checks; existing scoped repository tests retain role-loss and access-denial checks. Existing customer pairing, live cart, money and identity tests remain unchanged. Auth/widget tests that exercised demo routing now inject manager mocks explicitly.

Commands, run from `app/`:

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

## Required live Firebase / physical-device checkpoint

The user reports Stage 8 physical customer pairing and ADD/REMOVE verification succeeded. Stage 9 manager integration has not been physically verified by this implementation. Offline tests do not prove deployed rule access or device reconnect behavior.

1. Use an authorized test manager account whose Firebase token has `role: manager`; sign in again after an administrator changes claims. No credential or claim provisioning is included in Flutter.
2. Launch on the physical Android device using the existing Firebase setup. Manager reads need no pairing API base URL. Supply the existing Stage 8 `SMARTCART_API_BASE_URL` define if checking customer pairing in the same build.
3. Confirm the manager dashboard shows actual RTDB session keys, cart/customer identities, start/end times and backend bill totals, with no demo banner or fabricated alerts.
4. Through the existing authorized backend/cart lifecycle, create an active session. Confirm it appears without refresh. Produce ADD and REMOVE; verify quantity, unit/line prices, total and neutral activity against backend data.
5. Complete the session through the backend (`end_time`); confirm it leaves Active Carts and appears under Completed Sessions with retained bill/activity. Also check empty bills, empty event logs and no-session states in an approved test dataset.
6. Logout and sign in as another manager, then as a customer. Confirm old manager data disappears, no stale callbacks restore it, and customer UID-scoped discovery and secure pairing still work.
7. Verify a customer/non-manager cannot initiate manager collection reads. Have an authorized administrator revoke a test manager claim, refresh/sign in again, and confirm access is removed; do not modify rules to make this pass.
8. Exercise permission denial and network loss/reconnect on-device. Verify safe feedback and retry recovery. Firebase can retain a previously delivered snapshot while offline; the UI has no connectivity/freshness indicator and does not claim that a snapshot is current.

Remaining limitations: the authorized manager reads the complete session collection and retained activity without pagination; large histories need a separately reviewed scaling design. No product catalog, alert backend, theft detection, editing, checkout, write actions or connectivity indicator is added. Firebase claim revocation depends on token refresh and rules evaluation, not immediate push notification. Backend/rules/credentials and the customer implementation are untouched.

## Validation results

- `dart format .`: passed (43 Dart files).
- `flutter analyze`: no issues found.
- `flutter test`: **156 tests passed**, including all 148 Stage 8 tests and 8 new manager tests. Paused-subscription identity loss and production empty-dashboard states are also covered.
- `flutter build apk --debug`: succeeded; APK at `build/app/outputs/flutter-apk/app-debug.apk`. Non-fatal Java native-access and FlutterFire Kotlin migration warnings remain.
- `git diff --check`: passed; `git status --short`, `git diff --stat`, and `git diff -- app/` inspected.

Branch remains `feature/flutter-app`, HEAD `080b8ed`. Changes are uncommitted under `app/`; root `.idea/` remains pre-existing and untouched. No merge, rebase, cherry-pick, commit or push was performed. Live Firebase manager verification remains pending.
