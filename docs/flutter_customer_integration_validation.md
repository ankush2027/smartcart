# Flutter customer integration validation — 2026-10-08

Branch: `feature/flutter-customer-integration`.
Merge inputs: checkout recovery `a18e47e` and onboarding `5a02d07`.
The existing pending merge was resolved in place; neither feature history nor main was rewritten.

## Changes and findings

- Resolved all three customer screen conflicts by retaining the demo-payment flag,
  camera builder, cached cart, scanning state, and both session-listener behaviors.
- Confirmed a checkout teardown path: HTTP pairing/checkout requests force a Firebase
  token refresh; the auth repository previously published null even for the same UID.
  The guarded route then removed the customer screen and disposed its checkout state.
  Same-user refresh now retains the screen while claims resolve. Account changes,
  logout, refresh failures and stale asynchronous results remain guarded. No delays
  were added to application code. Automated tests verify the screen instance survives.
- Clear the remembered session and cached cart when dismissing a completed receipt,
  preventing app resume from reopening that old checkout. Regression covers payment,
  session disappearance, return to pairing and background/resume.
- Fixed the incoming Gradle signing validator's Kotlin `java.io.File` resolution error
  with an explicit `File` import. Release still requires ignored local signing material.
- Generated and retained the dependency lock for mobile_scanner 7.4.2. Its Android
  minimum API is 23, matched by the app configuration. Ignore generated Gradle reports.

## Validation results

- Flutter 3.47.5 stable; Dart 3.13.4.
- `flutter pub get`: passed.
- Scoped `dart format` on files edited during integration: passed.
- `flutter analyze` from the app directory: no issues.
- Final full `flutter test`: 230 passed, zero failures or skipped tests.
- Explicit targeted run of signup_test, cart_qr_test, customer_live_test,
  checkout_test, checkout_recovery_test and demo_integration_test: 80 passed.
  The final full suite includes these files with the added receipt lifecycle check.
- An intermediate new lifecycle test timed out because its broadcast fixture emitted
  before asynchronous cancellation finished. The fixture now flushes that operation
  and asserts the replacement listener exists before emitting. Final run passes.
- Signup tests cover validation, confirmation, duplicate submissions, sanitized errors,
  display name, authenticated routing and logout. Existing manager routing tests pass.
- Scanner tests cover the implemented plain CART-001 format, invalid payloads,
  synchronous duplicate rejection, disposal callbacks and manual fallback.
  Manual IDs intentionally retain backend validation rather than imposing the QR grammar.
- Cart/checkout tests cover owner changes, supplied totals, retained receipt items,
  uncertain network outcomes, cancellation, idempotency, stale results and demo opt-in.
  These are automated fake-backed tests, not live Firebase or physical-device tests.
- No new credential patterns found in added content; existing Firebase configuration
  is unchanged. Keystores, key.properties, APKs and build outputs remain ignored.

## Android and environment

Application ID remains `com.smartcart.app`. Camera and internet permissions are
present; camera hardware is optional. Debug permits local HTTP; release denies
cleartext. API URL remains configured with SMARTCART_API_BASE_URL, not source code.

The current Mac en0 address was checked during this run. There was no listener on
port 5001 and an HTTP probe to that LAN address failed to connect. ADB listed no
attached devices. No backend, database, hardware or Firebase console changes were made.

APK build: succeeded (`flutter build apk --debug` with the confirmed current Mac
LAN URL on port 5001 and SMARTCART_DEMO_PAYMENT=true).

Artifact: `/Users/Ankush/Desktop/smart-cart-flutter-e2e/app/build/app/outputs/flutter-apk/app-debug.apk`

SHA-256: `29addbb88ca2fb589732ac8bf40107e78622c8cc338aeda0e114500c279e7e52`.
Approximately 184 MiB, universal arm64-v8a/armeabi-v7a/x86_64 debug APK.
`apksigner verify --print-certs` passed: Android Debug signer. Packaged minimum API
is 24 (Flutter's higher default), target/compile API 36, version 1.0.0+1.
This is an explicitly demo-enabled private test artifact, not a production release.
Build emitted future Kotlin-plugin compatibility and native-access warnings;
they did not prevent this build. No release APK or live signing compatibility was tested.

## Remaining verification

1. Start the existing backend using its established configuration, listening on a
   LAN-reachable interface. Confirm the current Mac address and port; rebuild with
   that URL if it differs from the APK configuration.
2. Connect a phone to the same accessible Wi-Fi/hotspot and verify reachability from
   the phone. The API uses HTTP over the network and does not require USB for normal use.
3. Install the private debug APK; test real signup/login, camera permission denied
   and granted, invalid and valid QR, manual entry, occupied cart and duplicate scans.
4. Exercise real cart updates, totals, checkout cancellation/retry, network interruption,
   explicitly enabled demo payment, receipt, return to pairing and logout/login.
5. Verify customer B cannot see customer A's session or receipt using actual backend
   authorization and Firebase rules. Automated ownership checks do not replace this.
6. Production release remains separate: configure release signing and Firebase signing
   compatibility, use HTTPS and disable demo payment. No production readiness is claimed.
