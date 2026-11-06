# MacBook backend — Stage 1 preparation

Scope: `$HOME/Desktop/smart-cart-hardware`, branch
`feature/hardware-integration`. No tunnel, listener, live Firebase operation,
package installation, commit, push or merge was performed.

## Database decision and preservation

The original `.env` references a database outside this worktree. It is preserved
unchanged. The new `edge.cloud.local` launcher reads only the Firebase URL, Admin
credential path and allowed-cart setting from that file, treating them as data
without shell execution or interpolation. It explicitly selects the existing
`$HOME/Desktop/smart-cart-hardware/data/smartcart_demo.db`.

The local database was inspected with SQLite read-only/immutable mode: integrity
check returned `ok`; six catalogue products, two sessions and five bill lines were
present. No customer identifiers or contents are included here. No nonempty SQLite
journal/WAL sidecars were present during inspection. No database was created,
seeded, copied, reset, migrated or modified during Stage 1. These checks do not
establish that local session history matches the current remote RTDB namespace.

The launcher refuses missing databases and paths that resolve outside the hardware
workspace. It ignores inherited/legacy database, listener, proxy and demo settings,
so a previously exported shell setting cannot silently select the other worktree.

Pre-existing file hashes and original edited files are retained in the ignored
`.cloud-work/stage1-baseline/` directory. Original checkout/composition recovery
fixes and all Flutter files are unchanged. The original `.env` remains unchanged;
use the new launcher rather than sourcing the legacy `.env` into the generic runner.

## Listener, proxy and API boundaries

`edge.cloud.local` configures `127.0.0.1:5001`, never a LAN/public interface.
`edge.cloud.run` now defaults to loopback and accepts an explicit listen IP through
`SMARTCART_API_HOST`, with `PORT` controlling its port. The generic runner retains
explicit nonlocal binding for separately reviewed managed deployments.

Waitress trusts `X-Forwarded-Proto` only when the TCP peer is the exact configured
proxy IP. The local launcher selects `127.0.0.1`. Wildcards, CIDRs, DNS names and
lists are rejected. Other forwarded identity/host headers are cleared, one proxy
hop is configured, and tracebacks are not exposed. Flask does not apply ProxyFix
or independently trust client-supplied forwarded headers.

Only health probes work over ordinary local HTTP. Protected HTTP requests remain
rejected unless the trusted connector marks the original request as HTTPS. This
never replaces Firebase authentication. A local process can impersonate a loopback
proxy, so the MacBook and its local users are part of the trusted boundary; such a
process still requires valid Firebase authorization. Live Cloudflare header
normalization/overwriting must be verified in the next stage before exposure.

Firebase tokens are verified with `check_revoked=True`; customer/manager role and
session ownership checks remain in place. Browser-origin requests are denied;
native Android does not require CORS permission. Strict POST fields, request-size
limits, no-store responses and sanitized cloud request logs remain enabled.
Startup failures print a generic diagnostic, never SDK exception values. The
process lock is released even if initialization fails.

The old Render template now defaults demo payment off, explicitly specifies its
platform listener and no longer enables wildcard proxy trust. Its exact proxy
address must be resolved before that separate deployment path can be used. It is
not part of MacBook Stage 1.

## Firebase verification performed

The configured Admin credential file exists, has the required service-account
fields and a parseable private key. Its project ID matches the Android Firebase
client project, and the RTDB hostname is consistent with that project. Values,
keys and tokens were not printed or copied into source.

No token was exchanged and no remote Firebase read/write was made. These offline
checks do NOT prove current credential authorization, key revocation status,
remote database rules or connectivity. The credential remains at its existing
configured location; no other worktree's files were modified.

## Demo payment policy

Demo payment is disabled by default. It is a simulation, not proof that money was
received, and is unsuitable for real sales. For a controlled public project demo,
explicitly allow selected test customers with repeatable `--demo-customer-uid`
arguments. The cloud boundary enforces that allowlist on all fake-provider payment
routes, including payment creation, status, evidence and simulated completion.
Protecting only the simulate endpoint would leave the fake-evidence route exposed.
Normal role and session ownership checks still apply. Existing local legacy demo
behavior is preserved, but that older runner must not be exposed publicly.

When demo is off, existing pairing/checkout APIs remain available; no real payment
provider is wired into this composition. A complete simulated checkout demonstration
therefore needs explicitly approved test customer IDs. Never place Admin credentials,
tunnel credentials or any reusable service secret in the Android APK.

## Commands

Configuration-only check (standard Python; no socket, SDK or DB writes):

```sh
cd $HOME/Desktop/smart-cart-hardware
python3 -B -m edge.cloud.local --check
```

Future local startup, after approving live Firebase synchronization and verifying
that the chosen local history matches the RTDB namespace:

```sh
cd $HOME/Desktop/smart-cart-hardware
PYTHONDONTWRITEBYTECODE=1 $HOME/Desktop/smart-cart/.venv/bin/python -B -m edge.cloud.local
```

This uses the already-installed Python interpreter/dependencies read-only. The
hardware folder has no dedicated Python environment; no packages were installed
in this stage. Source, database and runtime writes are selected from the hardware
workspace. A dedicated environment can be prepared separately if desired.

For an explicitly approved simulated-payment customer, append
`--demo-customer-uid "$SMARTCART_TEST_CUSTOMER_UID"`; the variable must contain the
actual approved test customer's Firebase UID. Repeat for additional approved
customers. No UID is invented or stored in these instructions.

The startup command was NOT executed. Actual startup initializes the Admin SDK,
performs existing additive schema setup and starts the durable Firebase publisher;
it is not a read-only operation. Do not start it alongside an old backend/supervisor
publishing the same sessions. `--check` is the only command executed against real
local configuration during Stage 1.

## Regression evidence and remaining gates

Tests use mocked Firebase and temporary SQLite files under `.cloud-work`, never
existing application databases. New coverage checks local database selection,
missing/external database refusal, inert config parsing, listener validation,
Waitress peer-based header trust, spoofed forwarding rejection, revoked-token check
configuration, demo allowlisting and failed-startup lock cleanup.

Logs are `.cloud-work/stage1-backend-tests.log` and
`.cloud-work/stage1-final-verified.log`. The full backend suite passed 1,216 tests before final startup cleanup; the final
focused suite passed 54 tests after cleanup. An initial test-only logging mock
failed, was corrected, and passed in the final run. Configuration-only checks,
Python syntax and Git whitespace checks passed. Hash verification confirmed the
original `.env`, database and recovery fixes were unchanged.

Existing billing, receipt, cart turnover,
idempotent payment and projection-retry tests remain in the suite.

Before public HTTPS exposure:

1. Verify live Firebase access and deployed rules with approved test accounts.
2. Reconcile local/remote session identity deliberately; do not blindly publish
   local snapshots into an inconsistent RTDB namespace.
3. Keep only one authoritative database writer/publisher composition and review
   how local supervisor events join it. A tunnel does not provide that integration.
4. Configure a persistent tunnel hostname and test actual forwarded-header handling;
   no Cloudflare installation, account operation or hostname was created here.
5. Choose demo-only accounts and public abuse/rate controls. No new rate limiter
   was implemented in Stage 1.
6. Test two-phone ownership, token expiration, retries and disconnections through
   the real HTTPS route before distributing an APK.
7. Retain backups and arrange MacBook/backend/connector uptime. Missing physical
   weight integration, YOLO assets in this workspace and Android signing/build
   prerequisites remain separate blockers for a complete hardware demonstration.
