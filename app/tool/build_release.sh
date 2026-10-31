#!/bin/bash
set -euo pipefail
echo 'Standalone named-host builds are disabled. Run bash scripts/smartcart.sh demo to build with the current Quick Tunnel URL.' >&2
exit 1
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
URL="${1:-}"
fail(){ printf 'Release build blocked: %s\n' "$1" >&2; exit 1; }
[[ "$(git -C "$ROOT" branch --show-current)" == feature/hardware-integration ]] || fail 'expected branch feature/hardware-integration'
CONFIG="$ROOT/config/demo-deployment.conf"
HOST="$(PYTHONDONTWRITEBYTECODE=1 "$HOME/Desktop/smart-cart/.venv/bin/python" -B - "$ROOT" "$CONFIG" "$URL" <<'PY'
import sys
sys.path.insert(0,sys.argv[1])
from edge.cloud.deployment import parse_named_tunnel_config, validate_api_base_url
try:
 c=parse_named_tunnel_config(sys.argv[2])
 validate_api_base_url(sys.argv[3],c['HOSTNAME'])
 print(c['HOSTNAME'])
except Exception as e: raise SystemExit('Release build blocked: '+str(e)) from None
PY
)" || fail 'stable API URL validation failed'
command -v flutter >/dev/null || fail 'Flutter SDK is required'
cd "$ROOT/app"
flutter build apk --release --no-pub --dart-define="SMARTCART_API_BASE_URL=$URL"
printf 'APK built with stable API host %s at %s\n' "$HOST" "$ROOT/app/build/app/outputs/flutter-apk/app-release.apk"
