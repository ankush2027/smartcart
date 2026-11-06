# Controlled Android software demonstration

The secured launcher now supports `--software-input`. This reuses the existing product resolver, weight fixtures, billing, checkout generation fences and cart supervisor. Input stays in the local operator console; no public simulation API is added. Runtime projections serialize with the durable cloud publisher. Existing SQLite and Firebase records are not reset or repaired by this option.

## Activation (requires approval to stop/restart the existing backend)

Supply actual Firebase Authentication UIDs for both test customers in the process environment:

```sh
export SMARTCART_PREVIEW_UIDS='["CUSTOMER_A_UID","CUSTOMER_B_UID"]'
$HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local --check --software-input --demo-customer-uid CUSTOMER_A_UID --demo-customer-uid CUSTOMER_B_UID
# Only after restart approval and stopping the old service:
$HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local --software-input --demo-customer-uid CUSTOMER_A_UID --demo-customer-uid CUSTOMER_B_UID
```

The check does not start services or modify the database. Software input requires simulated payment enabled, all demo UIDs in the preview allowlist, and CART-001 in the cart allowlist. The default remains denial of protected APIs. Existing Firebase manager and session ownership checks remain enforced.

Use a native-client-compatible HTTPS endpoint with these protections enabled. Browser PIN authentication cannot be supplied automatically by this app. Do not embed Cloudflare service secrets in the APK. No tunnel or backend restart was performed during implementation.

## Build

Configure ignored `app/android/key.properties` with the intended release signing identity (`storeFile` absolute path, `storePassword`, `keyAlias`, `keyPassword`). Do not commit signing material. From `app`:

```sh
flutter build apk --release --no-pub --dart-define=SMARTCART_API_BASE_URL=https://APPROVED_API_HOST --dart-define=SMARTCART_DEMO_PAYMENT=true
```

Expected output after a successful build: `app/build/app/outputs/flutter-apk/app-release.apk`. An unset API endpoint fails closed and cannot demonstrate shopping. This path is not evidence that an APK was successfully built.

## Operator sequence

1. Customer A signs up/logs in. Signup does not automatically grant preview access; the verified UID must be configured before startup.
2. Scan a QR containing `CART-001`, or use manual entry. The authenticated backend pairs the cart; RTDB supplies the live bill.
3. Read the console's current session and generation. Submit `ADD <session> <generation> "Lays"`, `REMOVE <session> <generation> "Lays"`, then ADD again. Use only labels printed by the console, which come from the existing catalog. Do not fabricate hardware observations.
4. Start checkout in Android, start payment, then choose simulated payment. No real money moves. Receipt/recovery comes from backend checkout state.
5. Customer B logs in on the second phone and pairs CART-001 after completion. The next console runtime prints a new session; previous session/generation commands are rejected.

All product recognition and weights in this sequence are simulated. Physical QR camera behavior, real Firebase identity/RTDB delivery, two-phone behavior, load-cell calibration and camera recognition still require device validation.

## Verification on 26 October 2026

- Final full isolated backend suite: 1,248 passed, including the secured HTTP two-customer lifecycle. The configuration test checks that software mode fails closed without preview membership/cart approval.
- Full Flutter suite after error-handling fixes: 240 passed. Static analysis: no issues. These use fake authentication/network/RTDB boundaries, not real phone validation.
- Earlier release build failed because signing was not configured. That blocker has now been repaired with a new private demo release key; see the signing status below. The final API URL is still pending, so no usable release APK is claimed.
- An approved native-compatible HTTPS endpoint and actual customer UIDs are still required. The build attempt did not embed an endpoint. Existing Cloudflare browser PIN access is not sufficient for native HTTP calls.
- No commit, merge, push, reset, backend restart or tunnel change was performed.

## Retrieve the two customer UIDs

1. Open https://console.firebase.google.com/ and select `smartcart-16807` (the project configured in `app/lib/firebase_options.dart`). Confirm it is the same Firebase project used by the backend service account.
2. Open Authentication → Users (under Security in the current console navigation).
3. Search for the exact email address of customer A. Copy its User UID; repeat for customer B. If either account is absent, register it through the app first, then refresh Users. Do not substitute email addresses, RTDB session keys, access tokens, or service account IDs.
4. Confirm these are customer accounts, not accounts with the manager role claim. No role changes are required.
5. Replace the two placeholders in the `SMARTCART_PREVIEW_UIDS` JSON array and the matching `--demo-customer-uid` arguments above. Until then, leave the variable unset or set to `[]`. Do not store account passwords or tokens in this configuration.
6. Run only the `--check` command first. The allowlist is read at startup, so applying it requires an approved backend restart; changing it in a different terminal does not update the running process.

Firebase reference: https://firebase.google.com/docs/auth/android/manage-users

## Temporary Android-compatible Quick Tunnel (do not run before approval)

After the secured backend has been restarted with approval and its local security checks have passed:

```sh
cloudflared tunnel --protocol http2 --url http://127.0.0.1:5001
```

Omit `--allowed-mail`; that flag adds interactive PIN authentication which native Android API calls cannot satisfy. Use the new HTTPS URL printed by this process. The old URL is not configured anywhere by this change. This shell could not locate `cloudflared`; locate the executable used by the previous successful tunnel and use its absolute path, or install it from Cloudflare's official download instructions before running the command. Do not interrupt an existing tunnel to locate it.

Anyone with the URL can reach the server, including health checks. Protected shopping calls still require a verified Firebase token, preview UID membership, correct role and session ownership. Rate limits bound request admission but cannot prevent all denial of service. Keep the demo short; stop this new process when finished. Quick Tunnel URLs change on recreation and have no uptime guarantee. RTDB connections are direct to Firebase and retain their existing rules; the HTTP preview allowlist does not change those rules.

Cloudflare reference: https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/

## Rebuild with the newly printed URL

After configuring the release signing credentials described above:

```sh
cd $HOME/Desktop/smart-cart-hardware/app
read -r 'SMARTCART_DEMO_API_URL?Paste the NEW HTTPS Quick Tunnel URL: '
flutter build apk --release --no-pub \
  --dart-define="SMARTCART_API_BASE_URL=$SMARTCART_DEMO_API_URL" \
  --dart-define=SMARTCART_DEMO_PAYMENT=true
```

This is zsh syntax for the user's Mac shell. Install the resulting APK on both phones. Keep that tunnel process running throughout the demo; a new tunnel URL requires rebuilding and reinstalling this build-time-configured APK. No Cloudflare secret belongs in the app.

## Release signing status (26 October 2026)

No existing release keystore was found in this project. A new private demo signing key was generated using Java keytool: RSA 3072, JKS format, validity 10,000 days, alias `smartcart-demo-release`. The existing Gradle release configuration reads it from `app/android/key.properties`. No application ID or Firebase configuration was changed.

- Keystore: `app/android/.signing/smartcart-demo-release.jks`
- Private configuration: `app/android/key.properties`
- Both files have owner-only permissions (0600); the signing directory has 0700 permissions. Both paths are ignored by Git and are outside Android/Flutter asset directories. Passwords were generated randomly and were not printed or passed as command-line literals.
- Preserve this keystore and its private properties in a secure backup; future updates signed with this identity need the same key. Do not commit or share the files.
- Validation passed: Gradle `:app:validateReleaseSigning` and `:app:validateSigningRelease` completed successfully. Keytool also signed a local certificate request, confirming private-key access; that temporary request was removed. App source/assets were checked for signing files and the generated password.
- The final HTTPS endpoint is still required. No placeholder or old tunnel hostname has been embedded, and no new APK was produced in this signing preparation step.

Once the newly generated tunnel URL is available, use the rebuild command above. Successful output will be `app/build/app/outputs/flutter-apk/app-release.apk`. Verify the resulting APK signature and confirm signing secrets are absent from its archive before distributing it.
