#!/bin/bash
set -euo pipefail
echo 'Use the one-terminal orchestrator: bash scripts/smartcart.sh demo' >&2
exit 1

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
STATE_DIR="/private/tmp/smartcart-demo-${UID}"
TOKEN_FILE="$STATE_DIR/handshake"
fail() { printf 'SmartCart tunnel blocked: %s\n' "$1" >&2; exit 1; }
listener() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null; }

cd "$ROOT"
[[ "$(git branch --show-current)" == "feature/hardware-integration" ]] || fail 'wrong branch; expected feature/hardware-integration'
command -v lsof >/dev/null || fail 'lsof is required for listener verification'
command -v cloudflared >/dev/null || fail 'cloudflared is not installed or is not on PATH; install it before using the tunnel script'
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || fail 'private startup handshake is missing; start the backend with scripts/demo-backend.sh first'
[[ -d "$STATE_DIR" && ! -L "$STATE_DIR" && "$(stat -f '%u' "$STATE_DIR")" == "$(id -u)" && "$(stat -f '%Lp' "$STATE_DIR")" == "700" ]] || fail 'private handshake directory owner or permissions are unsafe'
[[ "$(stat -f '%u' "$TOKEN_FILE")" == "$(id -u)" && "$(stat -f '%Lp' "$TOKEN_FILE")" == "600" ]] || fail 'private startup handshake owner or permissions are unsafe'
for port in 5001 5002; do
  listener "$port" >/dev/null || fail "expected backend/attestation listener is missing on port $port"
done

"$HOME/Desktop/smart-cart/.venv/bin/python" -B - <<'PY'
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

token_path = Path(f"/private/tmp/smartcart-demo-{os.getuid()}/handshake")
token = token_path.read_text().strip()
if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
    raise SystemExit("SmartCart tunnel blocked: invalid local startup handshake")
expected = {
    "service": "smartcart-local-demo",
    "status": "ready",
    "listen_host": "127.0.0.1",
    "port": 5001,
    "verified_customer_admission": True,
    "demo_payment": False,
}
try:
    req = Request("http://127.0.0.1:5002/v1/attestation",
                  headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=3) as response:
        actual = json.load(response)
        if response.status != 200 or actual != expected:
            raise SystemExit("SmartCart tunnel blocked: backend attestation did not match the required local demo settings")
    with urlopen("http://127.0.0.1:5001/health/live", timeout=3) as response:
        if response.status != 200 or json.load(response) != {"status": "alive"}:
            raise SystemExit("SmartCart tunnel blocked: backend liveness check failed")
    with urlopen("http://127.0.0.1:5001/health/ready", timeout=3) as response:
        if response.status != 200 or json.load(response) != {"status": "ready"}:
            raise SystemExit("SmartCart tunnel blocked: backend readiness check failed")
except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
    raise SystemExit(f"SmartCart tunnel blocked: local backend check failed ({type(exc).__name__})") from None
print("Verified running SmartCart instance: loopback backend ready, all verified customers admitted, demo payment disabled.")
print("The attestation listener is bound to loopback port 5002 and is not the Cloudflare origin.")
PY

printf '\nStarting Cloudflare Quick Tunnel to the verified backend at http://127.0.0.1:5001.\n'
printf 'Keep this Terminal open; stop the tunnel with Ctrl-C.\n\n'
set -o pipefail
cloudflared tunnel --protocol http2 --url http://127.0.0.1:5001 2>&1 | awk '
  {
    print
    if (match($0, /https:\/\/[[:alnum:]-]+\.trycloudflare\.com/)) {
      print "\nSMARTCART DEMO HTTPS URL: " substr($0, RSTART, RLENGTH) "\n"
      fflush()
    }
  }
'
