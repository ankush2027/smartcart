# Controlled Android preview (local backend)

These controls apply to the cloud Flask composition. They do not restart the running server, change Flutter, start a tunnel, or modify Firebase rules/history. Existing processes continue using their previously loaded code until an approved restart.

## Admission and existing authorization

Every registered shopping, checkout, payment and manager route requires a Firebase ID token verified by Firebase Admin with revocation checking, then membership in `SMARTCART_PREVIEW_UIDS`. This is a JSON array of exact Firebase Authentication UIDs, not email addresses, cart IDs or caller-provided headers. Obtain UIDs from the authorized Firebase console. Maximum 100 entries; default `[]` denies all protected routes. Invalid configuration aborts startup. Managers must also be listed and retain a verified manager role claim. Listing a customer never grants manager privileges or another customer's session. Existing cart allowlist, ownership, active-session and idempotency checks remain in force.

Health endpoints remain unauthenticated and reveal only basic status. Unknown routes remain unavailable. The preview checks cover the cloud application, not other legacy launchers; use the local launcher below.

## Configuration and later startup

Do not source the legacy `.env` as shell code. The local launcher reads only approved Firebase/cart settings from it and selects the existing hardware-workspace database. Preview/rate settings below come from the process environment, not `.env`.

After explicit approval to restart the old backend, in a new terminal:

```sh
cd $HOME/Desktop/smart-cart-hardware
export SMARTCART_PREVIEW_UIDS='["[REDACTED]","[REDACTED]"]'
export SMARTCART_RATE_GLOBAL_PER_MINUTE=120
export SMARTCART_RATE_GLOBAL_BURST=40
export SMARTCART_RATE_USER_PER_MINUTE=30
export SMARTCART_RATE_USER_BURST=10
PYTHONDONTWRITEBYTECODE=1 $HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local --check
# Start only after the previous process has been stopped with approval:
PYTHONDONTWRITEBYTECODE=1 $HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local
```

Replace placeholders with verified test-account UIDs; do not put tokens or service-account keys in this list. Unset preview UID configuration defaults to denial. The local launcher fixes the listener to `127.0.0.1:5001`, trusts only the loopback proxy's `X-Forwarded-Proto`, and leaves other forwarded identity headers untrusted. HTTPS enforcement remains unchanged. The trusted connector must supply the actual edge scheme; arbitrary local processes are inside this loopback trust boundary.

Demo payment remains disabled by default. The local launcher ignores a legacy demo enable flag. Do not pass `--demo-customer-uid` for this preview unless simulated payment is separately authorized. If later authorized, a demo customer must belong to BOTH the preview list and the existing separate demo-payment list. No real payment processor is introduced. A demonstration with payment disabled cannot demonstrate successful paid checkout.

## Rate limits

One process-wide token bucket admits requests before routing, HTTPS checks and Firebase verification: default burst 40, refilling 120/minute. This includes invalid/missing credentials, health checks and unknown paths. A second bucket per approved verified UID defaults to burst 10, refilling 30/minute. Token refresh does not reset a UID's allowance. Rejection returns HTTP 429, integer `Retry-After` seconds and `Cache-Control: no-store`. Clients must respect retries and retain existing idempotency keys for repeated mutations.

No raw `X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`, user-ID header or IP address is used as a rate-limit identity. Memory is bounded to one shared bucket plus at most 100 preallocated UID buckets, with locking for concurrent requests. Authorization headers above 8192 characters are rejected before Firebase verification. Existing 16 KiB body limits remain unchanged.

Configuration ranges: global refill 1–6000/minute, global burst 1–500; per-user refill 1–1000/minute, per-user burst 1–100. Zero does not disable limiting. Counters reset on restart and are not shared across processes/replicas. A flood can consume the shared allowance and temporarily deny legitimate users and health checks. Limits do not prevent transport-level exhaustion or replace ingress protections. Requests rejected before Flask are subject to the existing HTTP server limits instead. Validate expected scan/poll traffic before increasing limits.

## Android and Firebase boundaries before exposure

The current browser email/PIN Access flow is not automatically supported by native Flutter API requests. Resolve that separately before Android testing; never embed Cloudflare service credentials in an APK. Any approved API exposure must retain Firebase verification, preview membership, ownership, role checks and rate limits. No unrestricted tunnel is authorized by these code changes.

The HTTP UID list does NOT change Firebase Authentication registration or direct RTDB access. Existing Flutter repositories can communicate directly with RTDB. Existing Firebase rules/claims still determine those permissions: an unlisted manager could retain direct manager reads, and an unlisted customer could retain permitted own-history reads. Review deployed rules and intended accounts separately if the entire demonstration, rather than just shopping APIs, must be limited to preview users. Do not claim removing a UID here revokes Firebase itself.

Before an approved restart/public test: verify the selected database/remote projections, preserve a verified database backup, approve exact test UIDs and manager claims, and resolve the Access/native-client path. Then test real Firebase tokens for approved/rejected accounts, owner isolation and 429 responses against the restarted local process. Automated tests use isolated SQLite and simulated verifier/RTDB boundaries; they do not establish real Android or public-tunnel end-to-end success.

## Revocation

Remove the UID from `SMARTCART_PREVIEW_UIDS` and perform an approved backend restart. For immediate revocation of all preview API accounts, set it to `[]` and restart with approval. Configuration is a startup snapshot, not hot-reloaded. This leaves shopping history and receipts untouched. If separately enabled, remove the UID from the demo-payment list too. Firebase token revocation/RTDB rule changes are distinct actions requiring separate authorization. Closing an authorized temporary tunnel is a separate way to end remote access; this implementation does not operate tunnels.
