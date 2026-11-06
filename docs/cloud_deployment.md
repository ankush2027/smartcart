# SmartCart cloud preparation and approval gate

Stage 1 update: for MacBook hosting use `local_backend_stage1.md` and
`python -m edge.cloud.local`. The local launcher overrides the legacy database
setting safely, binds loopback, and disables demo payment unless test customers
are explicitly selected. No tunnel has been configured.

Worktree: `$HOME/Desktop/smart-cart-hardware`.
Branch: `feature/hardware-integration`. No public service or final APK exists from
this change. No live Firebase data, credentials or rules were accessed or changed.

## Audit and architecture decision

The recovered Flutter app already has Firebase signup/login, role routing, customer
and manager RTDB listeners, QR/manual pairing, checkout and labeled demo payment.
Pairing and checkout use `SMARTCART_API_BASE_URL`; there were no LAN IP literals
in their current source. Checkout lacked URL security validation and followed GET
redirects; both are now corrected. Release Android retains cleartext prohibition
and application ID `com.smartcart.app`. Existing signing validation is unchanged;
`app/android/key.properties` is absent, so a release build needs the existing key
configuration supplied locally, never a replacement identity.

SQLite is authoritative. Pairing, billing, checkout and payment use SQLite-specific
transactions, locks, placeholders, schema migrations and persistent receipts.
Keep one API process, one instance and a persistent local disk for this project.
PostgreSQL is not necessary for this limited deployment; it becomes necessary for
horizontal scaling/high availability. That migration needs separate transaction,
money, constraint, locking and recovery tests, not a connection-string substitution.
No database migration was run against an existing customer database.

The local supervisor and demo runtime currently write SQLite/Firebase directly.
They are NOT cloud writers. Camera, YOLO, serial and weight handling remain local
and unchanged. See `cloud_edge_protocol.md` for the proposed authenticated event
boundary and implementation gates. Until that adapter exists, cloud shopping input
is not connected to physical hardware. Do not expose a public manual add-product
endpoint as a substitute.

## Added cloud composition

`python -m edge.cloud.run` wraps the existing Flask routes; it does not start the
camera, supervisor or development demo console. Waitress is an internal HTTP
server behind the managed HTTPS proxy, never an internet-facing development server.

- Firebase verifies signatures, expiration and revoked tokens. Missing/expired
  credentials return 401. Customer routes reject manager/unknown role claims.
  Existing per-session ownership checks remain authoritative. Manager-only
  `/manager/service-status` reports service capabilities; the manager UI still
  reads RTDB through its existing manager rules.
- No browser CORS permission is granted. Native Android needs none. POST bodies
  have exact field sets and a 16 KiB limit. Redirects are not followed by Flutter
  authenticated checkout requests. Responses avoid secret/error internals.
- Structured request logs contain generated request ID, route template, method,
  status and duration; never request bodies, query values, bearer tokens or UID.
  Cloud startup suppresses legacy free-form logs. Keep platform access logging
  configured not to capture Authorization headers or request bodies.
- `/health/live` checks process response. `/health/ready` checks SQLite schema and
  projection backlog age; it does NOT prove Firebase permissions/connectivity or
  catalogue completeness. Both avoid account data. Readiness turns 503 after five
  minutes of pending synchronization, and is available to internal HTTP probes.
- Additive `cloud_sync_outbox` and triggers enqueue session/bill mutations in the
  same SQLite transaction. One serialized publisher sends whole snapshots and
  conditionally acknowledges their revisions. Failure leaves durable retry work.
  Restart rescans sessions. This projects bill/metadata, preserves existing RTDB
  event logs, and does not recreate missing historical event-log records.
- `cloud_demo_ledger` persists the existing simulated payment provider across
  restarts. Only `SMARTCART_DEMO_PAYMENT=true` plus `SMARTCART_DEMO_ALLOWED_UIDS` exposes demo completion to allowlisted test customers; the app must
  also be built with its demo flag. NO real payment gateway is configured. Never
  deploy this demo endpoint for a real-money shop.
- Cloud runner refuses to create a replacement database if the configured file is
  missing and holds an exclusive process lock. All writers/publishers must use this
  composition. Do not run a second server, legacy sync process or shared-disk replica.

## Recommended hosting — approval required

Use a paid Render Docker web service with one persistent disk and managed TLS.
The review template is `render.cloud.yaml` (select this path explicitly in Blueprint
setup). Choose the region and confirm the current compute + disk price in Render
before authorizing creation. `0.5c-512mb` is the small instance template, not a
capacity guarantee; load-test and increase memory if required. Keep automatic
service deployment and Blueprint Auto Sync off until changes are reviewed.

Render free services sleep after 15 idle minutes and cannot attach persistent disks;
free Postgres expires after 30 days. They are unsuitable for this SQLite deployment.
Persistent disks limit the service to one instance and prevent zero-downtime deploys.
TLS is managed for the service's real assigned domain; no domain is invented here.
Sources: https://render.com/docs/free, https://render.com/docs/disks,
https://render.com/docs/deploys, https://render.com/docs/tls,
https://render.com/docs/blueprint-spec.

### Decisions needed before external operations

Approve hosting account/region/paid resource cost, a staging Firebase project (or an
explicit production-data migration plan), permission to use its Admin credentials,
and how the reviewed source is delivered (no commit/push has been made). Approve
public DEMO payment exposure only for the project demonstration. Supply the existing
Android signing configuration locally when the actual endpoint is ready.

### Deployment steps after approval

1. Review/commit/push only approved source on the requested branch yourself or
   explicitly authorize that action. Do not publish `.cloud-work`, DBs, `.env`,
   service accounts, signing material or APK backups.
2. Build `Dockerfile.cloud`. Its dedicated Docker ignore file allowlists Python
   source and cloud dependencies, excluding app assets, model weights and secrets.
   No OpenCV, Torch, YOLO or serial dependency is installed in the cloud image.
3. Create the reviewed Render service/template with one disk at `/var/data` and
   internal `PORT` supplied by Render. Verify the disk directory is writable by
   UID 10001 before startup; arrange mount permissions in the platform setup if
   necessary. Never fix this by running the entire application as root.
4. Provision `/var/data/smartcart.db` BEFORE startup. Use a reviewed SQLite online
   backup or a fresh staging catalogue initialized offline using existing database
   tooling; do not copy a busy SQLite file with ordinary file-copy commands.
   Preserve IDs, checkout/payment history and matching Firebase session identity
   for any migration. Validate `PRAGMA integrity_check`, row counts, money totals,
   active owners and catalogue prices. There is deliberately no automatic seed or
   destructive reset command. The repository catalogue has measured-weight gates;
   do not bypass them or substitute invented product data.
5. Use a dedicated Firebase staging dataset with matching Flutter client config,
   or explicitly reconcile the existing namespace before migration. An empty
   SQLite DB paired with an old populated RTDB can reuse session IDs and leak data.
   Never combine them. Stop/quiesce old writers before a cutover.
6. Mount the approved server service account as Render secret file
   `firebase-service-account.json`; set `GOOGLE_APPLICATION_CREDENTIALS` to
   `/etc/secrets/firebase-service-account.json`. Set `SMARTCART_FIREBASE_URL` to
   the real RTDB URL belonging to the same project as Flutter Firebase Auth.
   Client `google-services.json`/Firebase options are not Admin credentials.
7. Set `SMARTCART_DATABASE_PATH=/var/data/smartcart.db`,
   `SMARTCART_ALLOWED_CARTS=CART-001`, and `SMARTCART_DEMO_PAYMENT=true` for the
   explicitly approved demo. `SMARTCART_TRUSTED_PROXY` now requires an exact trusted peer IP; wildcard trust
   is rejected. Render proxy addressing must be resolved separately before using
   that template. The MacBook local launcher trusts only the loopback connector. Do not port-forward the origin to the internet.
8. Preserve/review `configs/firebase_rules.json`: customers read only owned
   sessions; managers use a server-issued manager claim; clients cannot write bills.
   Do not deploy production rules as part of these instructions without approval.
   Verify signup provider enablement and server-issued roles in staging.
9. Start the service, verify its actual HTTPS `/health/live` and `/health/ready`,
   then authenticate and confirm RTDB read/write permissions. Check no Admin key
   appears in the image/app. Set up health/backlog alerts and offsite encrypted
   SQLite online backups. Test a restore in isolation and document acceptable
   downtime/data-loss limits; disk persistence alone is not a backup strategy.
10. Run the two-phone acceptance procedure below. Do not treat HTTP readiness as
    proof of a working end-to-end shopping workflow.

## Exact local commands

Run from the hardware worktree. Creating a venv/installing dependencies does not
require Firebase access. Actual startup uses credentials and requires approval.

```sh
cd $HOME/Desktop/smart-cart-hardware
python3 -m venv .cloud-work/cloud-venv
.cloud-work/cloud-venv/bin/python -m pip install -r requirements-cloud.txt
# After Docker Desktop is running:
docker build -f Dockerfile.cloud -t smartcart-cloud:review .
```

After approval, set the real configuration in the shell without printing keys:

```sh
export SMARTCART_DATABASE_PATH='/absolute/path/to/approved/persistent/smartcart.db'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/approved/service-account.json'
# Set SMARTCART_FIREBASE_URL to the actual project RTDB URL.
export SMARTCART_ALLOWED_CARTS='CART-001'
export SMARTCART_DEMO_PAYMENT='false'
# If explicitly enabling demo payment, also set SMARTCART_DEMO_ALLOWED_UIDS.
export PORT='5001'
.cloud-work/cloud-venv/bin/python -m edge.cloud.run
```

Direct HTTP is deliberately rejected for authenticated routes. Use an approved TLS
proxy for cloud integration; health probes work on localhost HTTP. The original
local demo runner remains available unchanged for existing offline development.

## Flutter verification and final universal APK — endpoint required

Current verification uses `.cloud-work/flutter` so original generated platform files,
existing APKs and caches remain intact. The same commands can run in `app` when
regenerating its build outputs is desired. No final APK was built in this task.

```sh
cd $HOME/Desktop/smart-cart-hardware/.cloud-work/flutter
$HOME/flutter/bin/flutter pub get
$HOME/flutter/bin/flutter analyze
$HOME/flutter/bin/flutter test
```

Only after an actual HTTPS endpoint is deployed/reachable and the existing signing
configuration is supplied in this build copy, set `SMARTCART_DEPLOYED_URL` to that
verified endpoint. Then:

```sh
curl --fail --max-time 20 "$SMARTCART_DEPLOYED_URL/health/ready"
$HOME/flutter/bin/flutter build apk --release   --dart-define=SMARTCART_API_BASE_URL="$SMARTCART_DEPLOYED_URL"   --dart-define=SMARTCART_DEMO_PAYMENT=true
# Verify application ID com.smartcart.app and the existing signing certificate
# with Android SDK apkanalyzer/apksigner before distributing.
# The default APK is universal (no --split-per-abi).
```

After successful verification, copy `build/app/outputs/flutter-apk/app-release.apk`
to a NEW timestamped `SmartCart-Universal-HTTPS-<timestamp>.apk` under this worktree's
artifacts directory using exclusive creation. Never overwrite a prior backup APK.
Do not package `.env`, server Admin JSON, SQLite data or device credentials.

## Two-phone acceptance and hardware gate

Use staging accounts A/B and a manager account. Phone A uses mobile data and phone B
an unrelated Wi-Fi network, both installing the identical signed APK. Sign up and
log in A, pair CART-001, and verify B is denied while occupied. Use the authenticated
edge adapter only AFTER it is implemented, or controlled isolated server test
fixtures (never a public bypass) to exercise billing. Verify stored catalogue price,
manager live update, duplicate UUID no-op and two intentional events producing two
units. Freeze checkout; interrupt/restart the app/server and retry payment with the
same request key. Confirm one completed receipt, preserved history, cart release,
then B pairs into an empty independent session. Verify B cannot read A's HTTP/RTDB
session. Test token expiry/revocation, manager/customer role restrictions, airplane
mode/reconnect, Firebase outage/recovery, and stale buffered events after turnover.

Automated tests use mocked token verification and RTDB references, real isolated
SQLite transactions, and the existing simulated payment pipeline. They do not prove
live signup, deployed RTDB rules, radio/network switching or physical hardware.
Arduino Mega/HX711 integration is NOT deployment-ready: protocol implementation,
serial adapter, physical calibration, recognition/weight validation and real-device
acceptance are still required. No physical recognition success is claimed.
