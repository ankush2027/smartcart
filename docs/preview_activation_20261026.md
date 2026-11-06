# Preview activation — pending explicit restart approval

## Observed state

Backend PID [REDACTED] listens only on 127.0.0.1:5001 and was started 23 October 2026, 22:13:17 local time, with `python -B -m edge.cloud.local` in this worktree. It predates the preview changes. The process launch environment did not show SMARTCART_PREVIEW_UIDS or rate-limit settings (process environment inspection is not a definitive dump of Python's current configuration). A bounded 45-request health probe returned 45 HTTP 200 responses and no 429. Plain HTTP shopping GET returned 400 https_required; trusted loopback X-Forwarded-Proto=https returned 401 authentication_required without credentials. These do not establish preview membership enforcement. Treat the current process as unsecured for preview exposure.

Stop the public Quick Tunnel with Ctrl-C in its terminal now. Leave the backend running until restart approval. Stopping a Quick Tunnel invalidates its URL; restarting it generally produces a different hostname. Do not assume [REDACTED_TUNNEL_URL] will remain available.

The configuration below passed `--check` with both supplied UIDs; no service/SDK/database writes were started by that check.

## Exact safe restart procedure (only after approval)

1. Confirm the new public tunnel is stopped. Confirm the listener is still the expected process, and inspect its identity again rather than blindly killing PID [REDACTED]:

```sh
lsof -nP -iTCP:5001 -sTCP:LISTEN
ps -p [REDACTED] -o pid=,lstart=,command=
```

2. Stop only the existing backend with Ctrl-C in its original terminal. Do not kill unrelated Python processes. Confirm port 5001 is free before continuing.
3. Preserve SQLite with a consistent SQLite backup, not a raw copy of a possibly WAL-backed file. From the project directory:

```sh
cd $HOME/Desktop/smart-cart-hardware
$HOME/Desktop/smart-cart/.venv/bin/python -B - <<'PY'
from pathlib import Path
from datetime import datetime
import os, sqlite3
os.umask(0o077)
source = Path('data/smartcart_demo.db').resolve()
folder = source.parent / ('preview-backup-' + datetime.now().strftime('%Y%m%dT%H%M%S'))
folder.mkdir(mode=0o700)
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(folder / 'smartcart_demo.db') as dst:
        src.backup(dst)
        assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
print('SQLite backup verified:', folder)
PY
```

This does not delete, repair or reset SQLite or Firebase history.

4. In the SAME terminal used to launch the replacement backend, set all relevant settings explicitly:

```sh
export SMARTCART_PREVIEW_UIDS='["[REDACTED]","[REDACTED]"]'
export SMARTCART_RATE_GLOBAL_PER_MINUTE=120
export SMARTCART_RATE_GLOBAL_BURST=40
export SMARTCART_RATE_USER_PER_MINUTE=30
export SMARTCART_RATE_USER_BURST=10
PYTHONDONTWRITEBYTECODE=1 $HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local --check --software-input --demo-customer-uid [REDACTED] --demo-customer-uid [REDACTED]
# Only following approval, successful check and verified free port:
PYTHONDONTWRITEBYTECODE=1 $HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local --software-input --demo-customer-uid [REDACTED] --demo-customer-uid [REDACTED]
```

Startup uses the existing database and Firebase project, performs additive schema setup, and starts the normal projection publisher. The console uses simulated product events and fake payments; it does not validate physical hardware.

## Verification after activation, before public exposure

- Check plain HTTP protected GET returns 400 https_required.
- Check loopback GET with X-Forwarded-Proto=https and no token returns 401; invalid tokens also return 401.
- Confirm bounded bursts yield 429 with Retry-After and Cache-Control: no-store, then allow quota to refill.
- Use actual Firebase ID tokens from login for both approved customer accounts. Never paste passwords or tokens into this document or command history. A UID alone is not an authentication token.
- For each approved account, use a read-only checkout-state GET: an owned session succeeds; a nonexistent session can return session_not_found after passing admission. Confirm no preview_access_denied for either approved customer.
- A real valid token for a separate, non-approved account must return 403 preview_access_denied. An invalid token's 401 does NOT prove UID admission works. Do not create an extra account without authorization.
- Using a known existing session, its owner's GET should succeed and the other customer's GET must return 403. Pairing/checkout mutations require the controlled demonstration step, not these initial read-only probes.
- Real-token and live ownership results remain pending until restart approval and authenticated test sessions are available. Prior automated tests cover these boundaries with isolated SQLite and mocked token verification.

Only after those checks and approval should a public Quick Tunnel start. Verify through its new HTTPS URL: health, missing-token rejection, approved and denied token behavior. Check HTTP requests redirect to HTTPS or refuse access; do not send bearer tokens over plaintext public HTTP. Cloudflare must provide X-Forwarded-Proto=https through the trusted loopback connector. Keep the backend bound to 127.0.0.1.

## APK command for the supplied URL

Signing is already configured and validated. If and only if this exact tunnel hostname is still active and verified:

```sh
cd $HOME/Desktop/smart-cart-hardware/app
flutter build apk --release --no-pub \
  --dart-define=SMARTCART_API_BASE_URL=https://[REDACTED_TUNNEL_URL] \
  --dart-define=SMARTCART_DEMO_PAYMENT=true
```

If the tunnel was stopped/recreated as instructed, replace the URL with the NEW printed HTTPS hostname. Expected successful output: `$HOME/Desktop/smart-cart-hardware/app/build/app/outputs/flutter-apk/app-release.apk`. No build or activation is claimed by this procedure.

## Activation completed with explicit approval — 26 October 2026

Backup was created and verified BEFORE stopping the old backend, as explicitly requested in the approval. SQLite backup: `data/preview-backup-20261026T201624/smartcart_demo.db`; quick_check=ok, 12 tables preserved. A verification manifest with SHA-256 and table counts is beside it.

Only PID [REDACTED] was sent SIGINT after rechecking its command and worktree. Port 5001 was confirmed free. Replacement PID [REDACTED] runs the exact two demo UID arguments, `--software-input`, and the explicit preview/rate environment above, in the same worktree and branch. It is attached to this chat's managed terminal session ([REDACTED]); it is not installed as a persistent OS service. Keep that session alive for the demo console.

Observed live results:

- GET /health/live: 200 alive; GET /health/ready: 200 ready.
- Plain HTTP protected GET: 400 https_required.
- Loopback trusted HTTPS forwarding, no token: 401 authentication_required; invalid token: 401 invalid_token. Secure responses include HSTS.
- Global burst exhausted after 40 total requests: 429 rate_limited, Retry-After: 1, Cache-Control: no-store. The probe stopped at its first rejection.
- Process environment confirms exactly the supplied two preview UIDs; global 120/minute, burst 40; per-user 30/minute, burst 10.
- Live SQLite quick_check: ok. Projection outbox pending: zero.
- No cloudflared process running; no public tunnel started.
- 127 isolated tests passed: preview security, local backend, secure two-customer demo flow, session ownership, and RTDB authorization contract. Test data is disposable; verified-token behavior in tests uses injected verifiers.

Still pending: real Firebase ID tokens for both approved customers and one non-approved customer to prove live acceptance/rejection and owner isolation. Malformed-token rejection alone is not proof of live Firebase account authorization. No passwords/tokens were requested in chat or manufactured. Public HTTPS/Cloudflare behavior remains untested because the tunnel is stopped. Physical weight/recognition remains unvalidated; demo events/payments are simulated.
