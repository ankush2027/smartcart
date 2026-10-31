# Checkout/payment E2E fix — 8 October 2026

Changes are confined to the existing backend and Flutter E2E worktrees. Both checkouts were detached HEADs. No branch merge, database reset, Firebase modification, backend restart, or device deployment was performed. Existing edits in `app/test/demo_integration_test.dart` were retained.

## Findings and root-cause limits

The physical incident could not be reproduced: `adb devices -l` returned an empty device list. The suspected Firebase status flip is **not established** by the source: Flutter's `lib/core/data/session_mapper.dart:130` derives active/completed from `end_time`; checkout freeze alone does not complete the session.

The code did contain these reliability defects:

- `app/lib/features/customer/screens/customer_screen.dart`: checkout was rendered only within the ready/active live-cart branch, beneath the shopping content. There was no dedicated checkout transition. Firebase loading/error/no-session updates could replace in-progress payment UI. Current lines 149–201 render an independent checkout view; lines 59–77 recover new sessions and app resumes.
- `app/lib/features/checkout/controllers/checkout_controller.dart`: no use of the existing authoritative checkout/payment read endpoints for restart or ambiguous POST recovery. Failed requests returned to local shopping/frozen states; the next tap supplied a new key. Current lines 59–165 implement recovery and retain request keys; checkout and payment POSTs reuse those keys at lines 213 and 331.
- `app/lib/features/checkout/widgets/checkout_panel.dart`: cancellation was offered after payment reservation even though SQLite forbids it. The payment view now offers status refresh and explains the restriction, around line 176.
- `app/lib/features/checkout/models/checkout_models.dart`: unknown checkout states silently became FROZEN, and absent payment amounts became zero. Parsing now rejects these cases, around lines 63 and 124.
- `edge/billing/checkout.py:287`: recovery returned checkout totals but no receipt lines. Owner-authorized reads now include names, quantities, and exact paise amounts from SQLite.
- `edge/demo/composition.py:102`: completed simulation retries still depended on the process-local fake provider ledger. Verified/completed attempts now finish/replay through the existing service; completed retries also retry Firebase projection delivery without another capture.

The original installed-app freeze remains unverified; these are confirmed code defects and regression-tested fixes, not a claim of physical-device reproduction.

## Exact files changed

Backend root: `$HOME/Desktop/smart-cart-e2e`

- `edge/billing/checkout.py`
- `edge/demo/composition.py`
- `tests/test_checkout_receipt_recovery.py` (new)
- `docs/checkout_e2e_fix.md` (this report)

Flutter root: `$HOME/Desktop/smart-cart-flutter-e2e`

- `app/lib/features/checkout/controllers/checkout_controller.dart`
- `app/lib/features/checkout/models/checkout_models.dart`
- `app/lib/features/checkout/widgets/checkout_panel.dart`
- `app/lib/features/customer/screens/customer_screen.dart`
- `app/test/checkout_test.dart`
- `app/test/checkout_recovery_test.dart` (new)
- `app/test/demo_integration_test.dart` (only the failed-payment phase expectation was changed by this task; prior edits retained)

## Validation

- `flutter analyze --no-pub`: no issues.
- `flutter test --no-pub --reporter expanded`: 191 passed.
- Backend command below: 401 passed.
- `git diff --check`: clean in both worktrees.
- Plain `flutter analyze` also passed before subsequent edits. Plain `flutter test` could not resolve `pub.dev` during its dependency/advisory lookup; the full suite ran successfully using already-resolved packages with `--no-pub`.
- No physical phone verification: ADB listed no devices, including `[REDACTED]`.

Coverage includes checkout success/freeze, duplicate taps, dedicated widget transition, payment creation and demo completion, cancellation, network/retry recovery, stable checkout retry keys, restart during frozen checkout, stale-response fencing, receipt retention during Firebase release, supervisor generation fencing, and empty fresh customer sessions. Backend tests use temporary SQLite databases and fake Firebase references.

```sh
cd $HOME/Desktop/smart-cart-e2e
$HOME/Desktop/smart-cart/.venv/bin/python -m pytest \
  tests/test_checkout.py tests/test_checkout_generations.py \
  tests/test_payment.py tests/test_payment_recovery.py \
  tests/test_payment_schema_api.py tests/test_payment_money.py \
  tests/test_demo_software_integration.py tests/test_demo_firebase_wiring.py \
  tests/test_checkout_receipt_recovery.py -q

cd $HOME/Desktop/smart-cart-flutter-e2e/app
$HOME/flutter/bin/flutter analyze --no-pub
$HOME/flutter/bin/flutter test --no-pub
```

## Safe launch commands

Use the existing configured backend terminal and existing demo database. Stop the old backend normally before launching its replacement; do not run two supervisors against the same cart. Preserve its current `SMARTCART_FIREBASE_URL` and `GOOGLE_APPLICATION_CREDENTIALS` environment values. If its database differs from the path below, use that same existing path.

The file guard prevents startup from creating a missing database. No preparation or projection-cleanup flags are used.

```sh
cd $HOME/Desktop/smart-cart-e2e
: "${SMARTCART_FIREBASE_URL:?Keep the existing Firebase URL configured}"
: "${GOOGLE_APPLICATION_CREDENTIALS:?Keep the existing credential path configured}"
test -f $HOME/Desktop/smart-cart-e2e/data/smartcart_demo.db && \
$HOME/Desktop/smart-cart/.venv/bin/python -m edge.demo.run_demo \
  --db-path $HOME/Desktop/smart-cart-e2e/data/smartcart_demo.db \
  --cart-id CART-001 --host <LAN_HOST> --port 5001 \
  --with-supervisor --software-input
```

Connect and authorize the phone, then run:

```sh
cd $HOME/Desktop/smart-cart-flutter-e2e/app
$HOME/flutter/bin/flutter run --no-pub -d [REDACTED] \
  --dart-define=SMARTCART_API_BASE_URL=http://<LAN_HOST>:<PORT> \
  --dart-define=SMARTCART_DEMO_PAYMENT=true
```

## Manual Android verification

1. Confirm the phone reaches the configured backend IP on the same network. Log in and pair CART-001.
2. In the backend software console, issue `STATUS`; use its current session and generation in `ADD <session> <generation> "Lays"`. Confirm the full catalog name, quantity, and total in Flutter.
3. Tap Checkout twice quickly. Expect loading, then the dedicated checkout summary, Pay Now and Cancel Checkout; only one checkout identity should exist. DRAINING is expected while frozen.
4. Cancel before Pay Now. Wait for backend confirmation, then `STATUS` and ADD using the new generation. Shopping must resume; old-generation input must remain rejected.
5. Checkout again. Force-stop/reopen Flutter while frozen. Expect the recovered summary and Pay Now without another checkout POST.
6. Disable networking during checkout/payment requests. Expect a visible error or status retry; reconnect and retry. Verify the same checkout/payment identity rather than another charge.
7. Pay Now, then Complete Demo Payment. Expect success only after the backend confirms COMPLETED, with the correct item summary and paid amount. Payment reservation disables cancellation by design.
8. Confirm SQLite session completion and Firebase `end_time` agree, and the supervisor releases the old runtime. Return to pairing, log in as a different customer and pair CART-001. Expect a new empty session with no prior receipt/payment state; ADD should work using its session/generation.

## Remaining limitations

Live LAN, Firebase delivery, and Android rendering still require the above physical test. Payment success remains SQLite/backend-authoritative; Firebase outages can require replay of the completed demo endpoint or existing controlled reconciliation. A full backend restart loses the FakeProvider's in-memory ledger for an unfinished payment; completed retries are fixed, but persistence of unfinished fake orders is outside this change. Real payment-provider UI remains the project's existing unwired integration. Cancel after payment reservation remains intentionally forbidden by the existing backend contract. Restart recovery here covers a still-active/frozen session; it does not add a historical receipt browser after completed sessions have left the active Firebase query.
