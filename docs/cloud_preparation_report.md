# Cloud preparation report — 2026-10-23

Status: safe local preparation complete; NOT deployed and NOT end-to-end verified.
Branch verified: `feature/hardware-integration`, hardware worktree only.
No commit, push, merge, branch switch, live Firebase operation or APK build occurred.

## Changes in this task

Modified existing files (original bytes preserved under `.cloud-work/baseline`):
- `app/lib/features/customer/services/cart_pairing_service.dart`: shared URL validation.
- `app/lib/features/checkout/data/http_checkout_repository.dart`: shared URL validation,
  release HTTPS requirement, authenticated GET redirect refusal.

Created source/configuration:
- `app/lib/core/config/api_configuration.dart`
- `app/test/cloud_api_configuration_test.dart`
- `edge/cloud/__init__.py`
- `edge/cloud/app.py`
- `edge/cloud/outbox.py`
- `edge/cloud/demo_provider.py`
- `edge/cloud/run.py`
- `tests/test_cloud_preparation.py`
- `requirements-cloud.txt`
- `Dockerfile.cloud`
- `Dockerfile.cloud.dockerignore`
- `render.cloud.yaml`
- `docs/cloud_deployment.md`
- `docs/cloud_edge_protocol.md`
- `docs/cloud_preparation_report.md`

Local-only artifacts: `.cloud-work/.gitignore`, baseline file hashes/backups,
Flutter verification copy and test logs. This directory ignores all its contents.
No source `.gitignore` was changed. A hash comparison covered 282 tracked and
nonignored pre-existing files: none missing; only the two intended Flutter files
changed. This comparison does not inventory ignored files. Original platform files
and APK build directories were not used as build outputs. The existing dirty fixes
in `edge/billing/checkout.py` and `edge/demo/composition.py` are byte-for-byte preserved.

## Verification

- Existing checkout receipt/demo regression subset: 22 passed.
- Full backend suite: 1,191 passed in 57.22 seconds, including the first six new cloud tests.
- Final cloud-specific suite: 8 passed in 0.93 seconds after adding startup/readiness checks.
  The full suite was not rerun solely for these two additional tests.
- Flutter dependency resolution: passed offline using cached dependencies.
- Flutter analysis: no issues.
- Flutter tests: 238 passed (230 existing plus eight configuration tests).
- Cloud imports checked with NumPy/OpenCV/Torch/Ultralytics/serial blocked: passed.
- `git diff --check`: passed. New files are untracked and were validated by tests/analysis.
- Docker CLI present, daemon unavailable: image build and container startup NOT tested.

Logs: `.cloud-work/backend-tests.log`, `.cloud-work/cloud-tests.log`,
`.cloud-work/flutter-pub-get.log`, `.cloud-work/flutter-analyze.log`,
`.cloud-work/flutter-test.log`.

The offline cloud tests verify occupied-cart rejection, cross-customer HTTP access
denial, manager role gates, malformed/expired auth, HTTPS and CORS enforcement,
strict payload size/fields, repeated payment requests, persistent demo orders across
app reconstruction, receipt preservation, cart turnover, projection failure before
or after a remote write, conditional outbox acknowledgements during concurrent bill
changes, missing DB refusal and stale-sync readiness. They use actual isolated
SQLite operations and fake Firebase/token verification, never live customer data.

Flutter tests preserve existing auth/signup/navigation/customer/manager/recovery
coverage. Real Firebase signup/token refresh, deployed RTDB rules, physical phones,
network switching and actual hardware have NOT been tested by this task.
Duplicate edge events/offline edge replay remain design acceptance criteria; no
edge ingress exists yet and the test verifies it is disabled (404).

## Deployment readiness and blockers

See `cloud_deployment.md` for the audit, design tradeoffs, exact commands, hosting
configuration and two-phone plan. See `cloud_edge_protocol.md` for hardware boundary.

Recommend one paid Render Docker instance, managed TLS and persistent SQLite disk.
No PostgreSQL migration now; scale-out requires a separate migration and tests.
Cloud synchronization retries are durable; simulated payment provider state survives
restart. This is a project demo, not an integrated real-money payment provider.

Before public deployment: approve account/region/cost and source delivery; authorize
appropriate staging Firebase credentials/configuration; provision a reviewed database
without ID collision with RTDB; test container/disk permissions/backups and deployed
security. The actual HTTPS hostname does not yet exist in this task.

Final APK path: NONE — intentionally not built without a reachable stable endpoint.
The release app ID remains `com.smartcart.app`; signing configuration is unchanged,
but ignored `app/android/key.properties` is absent. Supply the existing signing
identity when building, preserve old APKs and save a new timestamped universal APK.

Hardware readiness: NOT ready for a full cloud-connected Arduino/HX711 demonstration.
Implement and prove the authenticated edge event protocol/atomic deduplication,
durable edge spool, supervisor adapter, physical serial integration and calibration
before claiming end-to-end hardware billing. Existing local hardware/runtime code
was preserved. No physical recognition or hardware success is claimed.
