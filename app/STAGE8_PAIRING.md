# Stage 8: customer pairing and live cart

Customer production composition now uses FirebaseCartRepository. Manager composition remains MockManagerRepository. Widgets contain no HTTP, Firebase Auth or RTDB calls.

Flow: authenticated customer → UID-scoped RTDB discovery → existing active cart, or Cart ID input → CartPairingService → fresh Firebase ID token → POST /pair-cart → backend verified UID and SQLite ownership → Firebase synchronization → live RTDB cart. The HTTP response is an acknowledgement, never a cart snapshot.

## Configuration and authentication

`SMARTCART_API_BASE_URL` is a required compile-time Dart define for pairing. It is trimmed, validated as an absolute HTTP(S) URL without credentials/query/fragment, normalized and suffixed with `/pair-cart`; an optional path prefix is preserved. There is no fallback. Missing configuration gives a controlled error when pairing is attempted; existing-session RTDB discovery still works.

The Firebase token provider calls `getIdTokenResult(true)` for each attempt and verifies the current UID has not changed. Requests contain only `{"cart_id":"<entered-id>"}` with `Content-Type: application/json` and `Authorization: Bearer <token>`. No UID/email is sent as authority. Tokens are not logged or persisted by this service, never enter query parameters, and are not forwarded through redirects. Firebase SDK handles its existing authentication session. Backend verification remains authoritative.

`paired` and `already_paired` with matching cart ID and a positive integer session ID acknowledge success. Known backend errors receive safe messages; integrity conflicts remain distinct from invalid input. Raw bodies and exceptions are never shown. Authentication rejection, token acquisition failure, network failure, 15-second token/request timeouts, malformed JSON and unexpected responses are controlled.

## State and lifetime

Discovery starts on screen entry. No active session displays Enter Cart ID and Connect Cart. Pairing disables duplicate submission and preserves input. Success waits on the existing RTDB subscription for up to 20 seconds. A synchronization timeout offers discovery retry, without automatically sending another pairing request. Initial discovery also has a 20-second bound. There is no cart polling. A cart arriving before the HTTP response takes precedence.

The stream renders cart/session identity, active status, items, quantities, supplied unit/line prices and final bill. Empty active carts are valid. Stage 7 integer-paise mapping and authoritative totals are unchanged. RTDB errors clear displayed data and offer retry. Recovered stream snapshots can restore the view.

UID-keyed screens dispose the old input, controller, timers and subscription. Stage 7 identity generations cancel old queries and reject stale events. Pairing checks identity before sending and accepting responses; disposed controllers ignore late completions. An already-transmitted request may finish at the backend after logout; logout never undoes ownership. No unpair endpoint or RTDB write is called.

## Physical Android checkpoint — required, not yet performed

Offline tests and an APK build do not prove live integration. Use authorized test accounts, a real active backend shopping session and the approved deployed rules/project `smartcart-16807`.

1. In a separate backend checkout containing current `main` pairing/runtime code, install its `requirements.txt`. Do not merge backend code into the Flutter branch. The following environment names and startup command were checked against `main:docs/network_boundary.md`:

   ```sh
   export SMARTCART_FIREBASE_URL="https://<approved-database-instance>"
   export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/service-account.json"
   export SMARTCART_API_HOST="0.0.0.0"
   export SMARTCART_API_PORT="5000"
   python -m edge.api.run_server
   ```

   Supply real values only in your local environment. Keep the service-account file outside Git and Flutter. Startup initializes Firebase and the existing SQLite schema before serving through Waitress. Host/port defaults are 0.0.0.0/5000.

2. Put the phone and edge computer on the same isolated development LAN; permit the configured port through the computer firewall. Obtain the computer's reachable LAN address. `localhost` on the phone is the phone; `0.0.0.0` is a bind address, not the client URL.
3. From `app/`, run on the connected physical Android device (replace the placeholder):

   ```sh
   flutter run --dart-define=SMARTCART_API_BASE_URL=http://<EDGE-LAN-IP>:5000
   ```

   Android main manifest denies cleartext; the debug manifest alone overrides it. Dart configuration also rejects HTTP outside debug. LAN HTTP is development-only and exposes Bearer tokens to interception: use an isolated test network. Production requires HTTPS/encrypted transport; this stage adds no TLS infrastructure.

4. Sign in as customer A with no owned active session. Confirm discovery finishes at the pairing form.
5. Use the backend's existing cart lifecycle to start an active, unassigned shopping session with a known Cart ID. Do not create fake assignment or bill data from Flutter.
6. Enter that ID and tap Connect Cart once. Confirm backend accepts the verified Firebase token and assigns the authenticated UID; do not print or copy tokens into logs. Confirm the UI waits for RTDB and then shows the owned cart/session.
7. Confirm an empty active cart shows an empty cart and bill, not the pairing form.
8. Produce a real backend ADD event using the existing edge workflow. Confirm items, quantity, line amount and total update without refresh. Produce REMOVE and confirm the corresponding update. Compare against backend authoritative values.
9. Log out. Confirm cart, input and pending state disappear. Log in again as A and confirm ownership is rediscovered without unpairing or mandatory re-pairing.
10. Log out and sign in as customer B. Confirm A's cart never appears. Attempt A's Cart ID and confirm ownership denial. Verify B's authorized scoped discovery with an independently owned session. Also test switching while a pairing request is pending.
11. Stop the API or disconnect networking and exercise retry; verify safe errors. Restore connectivity and confirm recovery. Exercise synchronization timeout if practical, confirming Retry only restarts discovery. Verify manager still shows its labeled mock dashboard.

Record device/build, backend revision, successful ADD/REMOVE observations and any failures without credentials. This checkpoint remains pending.

QR scanning, manager live monitoring (Stage 9), unpair, checkout, billing writes and ownership editing are deferred. Future QR input can supply the same cartId service parameter.

## Offline validation

Run from `app/`: `dart format .`, `flutter analyze`, `flutter test`, and `flutter build apk --debug`.

Tests inject HTTP clients, token providers, Firebase auth doubles and realtime repositories. They cover configuration, request contract, both success statuses, all backend errors, malformed responses, token/network failures, timeout, identity change, duplicate submission, live synchronization, empty carts, retry, cancellation and logout. Existing Stage 1–7 mapping, ownership, stale-event and manager regression tests remain offline. No Firebase project or running pairing server is needed.

Implementation validation: `dart format .` passed; `flutter analyze` reported no issues; all **148 tests passed**; `flutter build apk --debug` succeeded. Gradle emitted non-fatal Java native-access and FlutterFire Kotlin migration warnings. `git diff --check` passed. Physical-device integration remains unverified.

Change inventory (all under `app/`): created this guide, `lib/features/customer/services/cart_pairing_service.dart`, `test/pairing_service_test.dart`, and `test/customer_live_test.dart`. Modified README, the historical Stage 7 report note, main/debug Android manifests, app composition, routes, customer controller/screen, pubspec/lock, and existing auth/manager/widget tests to explicitly inject mock carts. `http` 1.6.0 is now a direct dependency (previously transitive). No backend/rules changes, credentials, personal IP addresses, commits or pushes.
