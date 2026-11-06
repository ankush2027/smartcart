# SmartCart one-command Quick Tunnel demo

The current demo workflow retains the local Flask/Waitress backend, loopback-only attestation, Firebase Authentication, SQLite, and Cloudflare Quick Tunnel. It does not use a named tunnel or a fixed hostname. Each Quick Tunnel start can receive a new URL, so the one-command supervisor rebuilds the APK with that run's URL.

## Run the whole demo

Open one Terminal and run:

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh demo
```

The supervisor checks `feature/hardware-integration`, configuration, existing ports, and duplicate Quick Tunnel processes. It starts the existing secure backend procedure, waits for its private attestation and local readiness, starts a Quick Tunnel only to `http://127.0.0.1:5001`, checks public readiness, resolves Flutter dependencies, builds a release APK with the assigned HTTPS URL, validates the APK and build receipt, and prints the URL/APK path. Keep the Terminal open for the demo. Press Ctrl-C or close the Terminal to stop the tunnel and backend children started by that supervisor.

The first run requires network access to Firebase configuration as already present locally, Cloudflare Quick Tunnel endpoints, and pub.dev for the new `google_sign_in` dependency. Flutter must be able to write its own SDK cache. If dependency resolution or build fails, the supervisor does not leave its backend/tunnel running.

The orchestrator verifies DoH and public HTTPS with the maintained `certifi` CA bundle because the configured macOS Python installation has no usable OpenSSL CA file or directory. Keep `certifi` installed in the configured Python environment; it is declared in the repository Python requirements.

Install `cloudflared` once if it is missing from PATH (for example, `brew install cloudflared`). The one-command workflow does not need a Cloudflare account or custom domain.

## Other commands

```sh
bash scripts/smartcart.sh status
bash scripts/smartcart.sh logs
bash scripts/smartcart.sh stop
```

`stop` signals only a running supervisor whose PID/command match the private state file. If it cannot verify the process, it sends no signal. Logs are in the ignored `logs/` directory. SQLite records, active cart assignments, receipts, Firebase data and APK files are preserved; each backend startup first creates its existing dated local database backup.

## Authentication setup needed once

Google sign-in code uses the official `google_sign_in` plugin and exchanges the Google ID token with Firebase Authentication. In Firebase Console, enable Google under Authentication sign-in providers and set the project support email. Register the Android package `com.smartcart.app` and the SHA-1/SHA-256 fingerprints for the release signing key (and debug key if debug builds will be used). Regenerate/download `google-services.json` so it contains Android OAuth configuration and a web OAuth client (`client_type: 3`). The repository's current file has no OAuth clients, so live Google sign-in will fail until this is done. Do not paste the signing key or fingerprints into source control.

The backend `POST /customer/register` endpoint verifies the Firebase bearer token with revocation checks, then idempotently stores only the verified UID in an additive SQLite `customers` table. The client sends an empty JSON object and cannot choose the UID. Email/password sign-in and restored sessions use the same provisioning step. Firebase owns credential uniqueness; repeat Google sign-in returns the same provider identity and the backend upsert does not create duplicate customer rows. Separate sign-in providers sharing an email remain subject to Firebase's account-linking policy.

The existing local launcher admits any Firebase-verified customer. It does not disable token verification, customer role checks, rate limiting, cart allowlisting, active-cart assignment checks, or session/bill ownership. `CART-001` remains assigned; use an eligible free cart such as `CART-002`. Demo payments remain disabled.

## Safety and verification boundaries

No service is started by status/log commands. The supervisor refuses ports 5001/5002 that are already occupied rather than killing a process. Port 5002 is never passed to Cloudflare. It does not claim live Google login or phone pairing from automated tests; verify both on the Oppo after Firebase Console setup. A Quick Tunnel URL is public to anyone who obtains it and changes on later starts, which is why every run prints the current URL and builds a matching APK.
