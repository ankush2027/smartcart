# Stage 13: customer onboarding, cart QR and Android APK

Implementation branch: `feature/flutter-customer-onboarding`, based on Flutter
E2E commit `9026714`. Backend compatibility: `feature/demo-e2e-fix` (`14a6f72`).
Keep the two histories separate. No backend change is required for signup or QR.

## Customer workflow

- First launch offers Sign In and Sign Up. Registration requires full name,
  email, password (at least six characters) and matching confirmation. Firebase
  enforces any stronger password policy configured for the project. Enable
  Email/Password in the existing Firebase project's Authentication console if
  it is not already enabled; no console changes were made in this stage.
- Signup calls Firebase `createUserWithEmailAndPassword`, then `updateDisplayName`.
  Only after profile/token resolution does the existing auth controller expose
  the user. Roles come exclusively from token claims, never form data. If account
  creation succeeds but profile setup fails, the UI directs the customer to log
  in; it does not delete the account or blindly repeat account creation.
- Returning customers use Sign In. Incorrect password and unknown account share
  a generic message. Firebase SDK persistence restores their own account on
  restart; there is no stored custom login flag/password/token. Token refresh
  errors clear local authorization. Revocation is not an instantaneous offline
  notification: Firebase refresh and backend authorization remain necessary.
- A customer without an active session chooses **Scan Cart QR Code** or manual
  **Enter Cart ID**. Login/signup never automatically calls `/pair-cart`.
- Pairing obtains a fresh ID token and posts only `{"cart_id":"CART-001"}`.
  Occupied/invalid carts, expired auth, server errors and network errors use safe
  messages. The existing controller blocks repeated submissions and waits for
  the owner's RTDB projection before displaying a cart.
- Live cart items, prices and totals remain backend-owned. Checkout freezes the
  session; cancellation restores shopping with backend generation replacement.
  Stage 12 string payment IDs, `FAILED` handling, demo opt-in, backend-only
  `COMPLETED` success and receipt persistence are preserved.
- After payment, use **Continue to cart pairing** for another shopping trip.
  Payment does not log the customer out. Logout cancels the visible customer's
  subscriptions and returns to authentication; it does not release/complete the
  backend cart. Another account must sign in explicitly. Manager claim routing
  is unchanged; signup contains no manager-role selector.

## Two different QR codes

**Cart label:** plain UTF-8 `CART-001`. Accepted scanner format is
`CART-` followed by 1–32 uppercase ASCII letters/digits, with no spaces/newlines.
URLs, JSON, session IDs, bearer tokens, URI query parameters and credentials are
rejected. The backend still decides whether the physical cart ID is allowed and
available. Manual entry retains the existing backend validation for other IDs.

Scanning uses pinned `mobile_scanner: 7.4.2`, rear camera, QR format only, bundled
ML Kit (no first-use model download). Camera operations are serialized across
app lifecycle events. Scanning stops before pairing; one accepted code consumes
the scanner. Invalid QR feedback offers retry. Permission denial/unavailable
camera offers Android Settings guidance and manual entry. No camera images,
QR payloads or ID tokens are logged or stored by this feature.

**Download QR:** points to the actual published download page, not `CART-001`.
It is for the phone's normal camera/browser; SmartCart intentionally rejects it
as a cart label. No download URL or APK currently exists from this stage.

## Mac validation before any APK distribution

Transfer all Stage 13 tracked edits AND new files into a separate working copy
at `9026714`, without copying Windows `.git`, `.dart_tool`, build directories,
credentials or virtual environments. Use the existing Mac Flutter installation.
The inherited project requires Dart `^3.13.4`; do not silently downgrade it.

From the Stage 13 Flutter worktree's `app` directory:

```zsh
flutter --version
flutter doctor -v
flutter pub get
git diff -- pubspec.lock
flutter analyze
flutter test
git diff --check
```

Dependency resolution is required once for the new scanner package. The lockfile
has deliberately NOT been fabricated on Windows: review its generated Mac diff
and include it in the eventual approved commit. Stop on resolver, analyzer or
test failures. Resolve/format any diagnosed issues on the Mac, then rerun the
complete suite. No success count is claimed for the new tests here.

New tests: `signup_test.dart`, `cart_qr_test.dart`; updated
`customer_live_test.dart` to target the manual pairing button and scroll it into
view now that a scanner button exists. Existing auth, manager, RTDB ownership,
account-switch, checkout and Stage 12 receipt tests must all continue to pass.

## Trusted Wi-Fi college demo

Use the Stage 12 backend guide in the **backend branch** to start the explicit
managed demo with valid backend-only Firebase configuration, software input and
controlled projection reconciliation if needed. Never run two independent
SQLite authorities against the same Firebase sessions namespace. Use the same
Firebase project on both sides. Do not routinely reset the database.

Connect the Android phone by USB with debugging enabled and authorize the Mac.
Phone and backend computer must share a trusted Wi-Fi/LAN; allow the backend
port through the local firewall without exposing it publicly. Use the backend
computer's LAN address, not `localhost` or `0.0.0.0` on the phone.

```zsh
flutter devices
read "PHONE_ID?Android device ID: "
read "BACKEND_IP?Backend computer LAN IPv4: "
flutter run --debug -d "$PHONE_ID" \
  "--dart-define=SMARTCART_API_BASE_URL=http://${BACKEND_IP}:5000" \
  --dart-define=SMARTCART_DEMO_PAYMENT=true
```

Validate new signup, duplicate email, bad credentials, app restart restoration,
manager login, camera denied/granted, app background/resume, invalid/download QR,
duplicate scans, manual fallback and occupied cart. Then run the Stage 12 flow:
scan `CART-001` → software ADD/REMOVE → freeze → cancel → new generation → final
checkout → explicit demo payment → authoritative receipt → RTDB completion and
runtime release → Customer B pairs an empty fresh session. Logout during scanning
and during pending pairing must remove A's UI/listeners and never display it to B.

For a downloadable **private demo debug APK** (same configuration):

```zsh
flutter build apk --debug \
  "--dart-define=SMARTCART_API_BASE_URL=http://${BACKEND_IP}:5000" \
  --dart-define=SMARTCART_DEMO_PAYMENT=true
mkdir -p build/distribution
cp build/app/outputs/flutter-apk/app-debug.apk \
  build/distribution/SmartCart-1.0.0-demo-debug.apk
```

This is debug-signed, permits LAN HTTP and uses fake money. Label it accordingly;
do not distribute it as a production shopping/payment app. A different LAN/IP
requires a rebuild. Normal release builds retain Android cleartext denial and
pairing's HTTPS-only policy. No broad release HTTP exception was added.

## Release signing and downloadable release APK

Application ID/namespace stay `com.smartcart.app`; existing Firebase Android
configuration is unchanged. Internet and camera permissions are declared, camera
hardware is optional for manual fallback, and minSdk is at least 23 (or Flutter's
higher default). Compile/target SDK follow the installed Flutter toolchain;
scanner requires compileSdk >=34. Verify the merged manifest and Gradle build on
the Mac with the project's existing AGP/Java toolchain. iOS/macOS camera packaging
is outside this Android milestone.

Release builds no longer fall back to the debug signing key. Create or reuse one
private signing key for all updates of this downloadable app. Keep encrypted
backups outside the repository; changing the key prevents in-place updates.

Example key generation (prompts for passwords; no password in shell history):

```zsh
keytool -genkeypair -v -keystore "$HOME/smartcart-release.jks" \
  -storetype JKS -keyalg RSA -keysize 2048 -validity 10000 -alias smartcart
```

Create ignored `app/android/key.properties` locally in a private editor:

```properties
storeFile=/absolute/private/path/to/smartcart-release.jks
storePassword=YOUR_PRIVATE_STORE_PASSWORD
keyAlias=smartcart
keyPassword=YOUR_PRIVATE_KEY_PASSWORD
```

Those are placeholders, not supplied credentials. Restrict file permissions and
never upload either file. Missing signing configuration deliberately fails release
builds. Inspect the eventual signing certificate with Android SDK `apksigner`
before distribution. Check any Firebase signing-certificate/App Check settings
against the chosen release key without changing the application ID/project.

For release, provision a reachable **HTTPS backend**. Ordinary release builds
leave demo payment off; the current normal backend has no real payment provider.
This stage adds neither Razorpay nor production payment activation. Real-money
shopping is blocked until a separately approved provider/backend is available.
Do not advertise a normal release as supporting real checkout payment today.

After Mac analysis/tests/device acceptance and signing setup:

```zsh
read "BACKEND_URL?Reachable HTTPS backend base URL: "
flutter build apk --release --build-name=1.0.0 --build-number=1 \
  "--dart-define=SMARTCART_API_BASE_URL=${BACKEND_URL}" \
  --dart-define=SMARTCART_DEMO_PAYMENT=false
mkdir -p build/distribution
cp build/app/outputs/flutter-apk/app-release.apk \
  build/distribution/SmartCart-1.0.0-1-release.apk
shasum -a 256 build/distribution/SmartCart-1.0.0-1-release.apk
```

Use an unused, monotonically increasing build number for later updates; version
name is customer-facing. `1.0.0+1` is the current source version, not proof it is
unused on distributed devices. The universal APK path above avoids ABI selection
confusion. A signed HTTPS **demo** release can explicitly set demo payment true,
but must be labeled as fake payment and use the explicit demo backend.

## Install, host and produce the download QR

Install locally with `adb -s "$PHONE_ID" install -r PATH_TO_BUILT_APK` or download
on Android, open the APK and grant "Install unknown apps" to the chosen browser
when prompted. Verify the source/checksum. An existing debug install with the
same package ID but different signing certificate cannot be updated in place;
uninstalling it clears local app data/login, not backend ownership.

After explicit publication approval, use the repository's GitHub **Releases**
page: draft a release for the reviewed commit/tag, attach only the built APK and
checksum, label debug/demo builds prominently, then publish. Do not attach any
source credentials, signing files or database. Confirm repository visibility
allows intended customers to download; otherwise use an approved public HTTPS
download host. Copy the real published page URL and test it signed out on Android.

With a trusted local `qrencode` utility available on the Mac:

```zsh
read "DOWNLOAD_PAGE?Actual published HTTPS download page URL: "
qrencode -o build/distribution/SmartCart-download-QR.png "$DOWNLOAD_PAGE"
printf %s 'CART-001' | qrencode -o build/distribution/CART-001-QR.png
```

Test the download QR with the phone camera, and the cart QR inside SmartCart.
No APK, release, download page or QR artifact was built/published on Windows.

## Verification status and sources

Windows: Flutter analyze **NOT RUN**, Flutter test **NOT RUN**, Android APK build
**NOT RUN**, physical Android test **NOT RUN**. Backend was not modified/tested
in this stage. Static review and Git checks are not compilation evidence.

- [Firebase password authentication](https://firebase.google.com/docs/auth/flutter/password-auth)
- [Firebase user profiles](https://firebase.google.com/docs/auth/flutter/manage-users)
- [mobile_scanner 7.4.2](https://pub.dev/packages/mobile_scanner/versions/7.4.2)
- [Flutter Android release guide](https://docs.flutter.dev/deployment/android)
